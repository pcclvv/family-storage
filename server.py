# -*- coding: utf-8 -*-
"""
家庭储物管家 · 后端服务
- 零依赖：只用 Python 标准库（http.server + sqlite3）
- 数据模型：房间 -> 柜子 -> 柜内条目；条目引用「物品基础库」，物品名称/分类统一维护
- 数据存 data/storage.db，多人访问同一份数据（增删改查全部即时共享）
- 监听 PORT 环境变量（默认 3000），绑定 0.0.0.0，便于云端发布
"""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
DATA = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA, "storage.db")

CATEGORIES = ["被子", "床单", "被套", "枕头", "毛毯", "其他"]   # 初始种类，之后可在页面上自己加
ROOM_PREFIX = ["A", "B", "C", "D", "E", "F", "G", "H"]
_db_lock = threading.Lock()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    os.makedirs(DATA, exist_ok=True)
    fresh = not os.path.exists(DB_PATH)
    c = conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS rooms (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          sort INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS cabinets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          room_id INTEGER NOT NULL,
          code TEXT NOT NULL,
          name TEXT NOT NULL DEFAULT '',
          sort INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS types (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          category TEXT NOT NULL DEFAULT '其他',
          sort INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS cats (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          sort INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cabinet_id INTEGER NOT NULL,
          type_id INTEGER NOT NULL DEFAULT 0,
          name TEXT NOT NULL DEFAULT '',
          category TEXT NOT NULL DEFAULT '其他',
          qty INTEGER NOT NULL DEFAULT 1,
          note TEXT NOT NULL DEFAULT '',
          who TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          who TEXT NOT NULL DEFAULT '',
          action TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '',
          at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_items_cab ON items(cabinet_id);
        """
    )
    migrate(c)
    ensure_cats(c)
    c.execute("CREATE INDEX IF NOT EXISTS idx_items_type ON items(type_id)")
    if fresh or c.execute("SELECT COUNT(*) n FROM rooms").fetchone()["n"] == 0:
        seed(c)
    c.commit()
    c.close()


def migrate(c):
    """旧版本（物品自带名称）升级为「物品基础库 + 柜内引用」"""
    cols = [r["name"] for r in c.execute("PRAGMA table_info(items)")]
    if "type_id" not in cols:
        c.execute("ALTER TABLE items ADD COLUMN type_id INTEGER NOT NULL DEFAULT 0")
    unknown = c.execute("SELECT * FROM items WHERE type_id=0").fetchall()
    for it in unknown:
        name = (it["name"] or "").strip() or "未命名物品"
        cat = it["category"] if it["category"] in CATEGORIES else "其他"
        row = c.execute("SELECT id FROM types WHERE name=?", (name,)).fetchone()
        if row:
            tid = row["id"]
        else:
            c.execute("INSERT INTO types(name,category,sort) VALUES(?,?,?)", (name, cat, 0))
            tid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        c.execute("UPDATE items SET type_id=? WHERE id=?", (tid, it["id"]))


def ensure_cats(c):
    """物品「种类」也是基础数据：空库时补默认几种，旧数据里出现过的种类自动补进来"""
    names = [r["name"] for r in c.execute("SELECT name FROM cats ORDER BY sort, id")]
    if not names:
        for i, n in enumerate(CATEGORIES):
            c.execute("INSERT INTO cats(name, sort) VALUES(?,?)", (n, i))
        names = list(CATEGORIES)
    for row in c.execute("SELECT DISTINCT category FROM types WHERE category<>''").fetchall():
        if row["category"] not in names:
            c.execute("INSERT INTO cats(name, sort) SELECT ?, COALESCE(MAX(sort),0)+1 FROM cats",
                      (row["category"],))
            names.append(row["category"])


def cat_names(c):
    return [r["name"] for r in c.execute("SELECT name FROM cats ORDER BY sort, id")]


def ensure_cat(c, name):
    """返回一个合法的种类名；库里没有就顺手补一条"""
    name = (name or "").strip()[:10]
    if not name:
        return "其他"
    if name not in cat_names(c):
        c.execute("INSERT INTO cats(name, sort) SELECT ?, COALESCE(MAX(sort),0)+1 FROM cats", (name,))
    return name


def seed(c):
    """两个房间、每间 5 个柜子，物品库预置常见床品，并放几件示例"""
    for name, sort in [("主卧", 1), ("次卧", 2)]:
        c.execute("INSERT INTO rooms(name, sort) VALUES(?,?)", (name, sort))
    rooms = [dict(r) for r in c.execute("SELECT * FROM rooms ORDER BY sort, id")]
    cab_ids = {}
    for idx, room in enumerate(rooms):
        prefix = ROOM_PREFIX[idx] if idx < len(ROOM_PREFIX) else str(idx + 1)
        for i in range(1, 6):
            code = "%s%d" % (prefix, i)
            c.execute("INSERT INTO cabinets(room_id, code, name, sort) VALUES(?,?,?,?)",
                      (room["id"], code, "", i))
            cab_ids[code] = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]

    lib = [("冬被", "被子"), ("夏被", "被子"), ("蚕丝被", "被子"), ("四件套", "被套"),
           ("床单", "床单"), ("被套", "被套"), ("枕头", "枕头"), ("毛毯", "毛毯")]
    tid = {}
    for i, (name, cat) in enumerate(lib):
        c.execute("INSERT INTO types(name, category, sort) VALUES(?,?,?)", (name, cat, i))
        tid[name] = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]

    ts = now_str()
    demo = [
        ("A1", "冬被", 2, "厚羽绒，压缩袋装"),
        ("A1", "四件套", 1, "灰色棉麻"),
        ("A2", "夏被", 1, "薄空调被"),
        ("A3", "蚕丝被", 1, ""),
        ("A4", "床单", 3, "1.8m 三条"),
        ("B2", "枕头", 4, "乳胶枕两只"),
        ("B2", "毛毯", 1, "珊瑚绒"),
        ("B5", "被套", 2, "备用"),
    ]
    for code, name, qty, note in demo:
        cat = dict(lib)[name]
        c.execute(
            "INSERT INTO items(cabinet_id,type_id,name,category,qty,note,who,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (cab_ids[code], tid[name], name, cat, qty, note, "示例", ts, ts))
    c.execute("INSERT INTO logs(who,action,detail,at) VALUES(?,?,?,?)",
              ("系统", "初始化", "已创建 主卧/次卧 各 5 个柜子，物品库预置 8 种常见床品", ts))


def log(c, who, action, detail):
    c.execute("INSERT INTO logs(who,action,detail,at) VALUES(?,?,?,?)",
              (who or "家人", action, detail, now_str()))


def get_state():
    c = conn()
    rooms = [dict(r) for r in c.execute("SELECT * FROM rooms ORDER BY sort, id")]
    cabs = [dict(r) for r in c.execute("SELECT * FROM cabinets ORDER BY room_id, sort, id")]
    items = [dict(r) for r in c.execute(
        "SELECT i.*, t.name AS tname, t.category AS tcat FROM items i "
        "LEFT JOIN types t ON t.id=i.type_id ORDER BY i.id DESC")]
    types = [dict(r) for r in c.execute("SELECT * FROM types ORDER BY sort, id")]
    cats_rows = [dict(r) for r in c.execute("SELECT * FROM cats ORDER BY sort, id")]
    cats = [r["name"] for r in cats_rows]
    logs = [dict(r) for r in c.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 12")]
    for it in items:
        if it.get("tname"):
            it["name"] = it["tname"]
            it["category"] = it["tcat"] or it["category"]
    usage = {}
    for it in items:
        usage[it["type_id"]] = usage.get(it["type_id"], 0) + 1
    for t in types:
        t["used"] = usage.get(t["id"], 0)
    c.close()

    by_cab = {}
    for it in items:
        by_cab.setdefault(it["cabinet_id"], []).append(it)
    for cab in cabs:
        cab["items"] = by_cab.get(cab["id"], [])
        cab["count"] = sum(i["qty"] for i in cab["items"])
    for room in rooms:
        room["cabinets"] = [x for x in cabs if x["room_id"] == room["id"]]
        room["count"] = sum(x["count"] for x in room["cabinets"])
    return {"rooms": rooms, "logs": logs, "types": types, "categories": cats, "cats": cats_rows}


class Handler(BaseHTTPRequestHandler):
    server_version = "StorageHome/2.0"

    # ---------- 工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        pass

    def _who(self, b=None):
        w = (b or {}).get("who") if isinstance(b, dict) else ""
        w = (w or "").strip()[:20] or "家人"
        return w

    # ---------- 路由 ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_file("index.html")
        if path.startswith("/static/"):
            return self._serve_file(path[len("/static/"):])
        if path == "/api/health":
            return self._send(200, {"ok": True, "time": now_str()})
        if path == "/api/state":
            return self._send(200, get_state())
        if path == "/api/search":
            q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0].strip()
            return self._send(200, self._search(q))
        return self._send(404, {"error": "not found"})

    def _serve_file(self, rel):
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        ctype = "text/html; charset=utf-8"
        if full.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif full.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif full.endswith(".svg"):
            ctype = "image/svg+xml"
        elif full.endswith(".png"):
            ctype = "image/png"
        with open(full, "rb") as f:
            return self._send(200, f.read(), ctype)

    def _search(self, q):
        c = conn()
        rows = c.execute(
            """SELECT i.id, i.qty, i.note, i.cabinet_id, i.type_id,
                      COALESCE(NULLIF(t.name,''), i.name) AS name,
                      COALESCE(NULLIF(t.category,''), i.category) AS category,
                      cb.code, cb.name AS cab_name, r.name AS room_name
               FROM items i
               JOIN cabinets cb ON cb.id=i.cabinet_id
               JOIN rooms r ON r.id=cb.room_id
               LEFT JOIN types t ON t.id=i.type_id
               WHERE (?='' OR COALESCE(t.name,i.name) LIKE ? OR COALESCE(t.category,i.category) LIKE ?
                      OR i.note LIKE ? OR cb.code LIKE ? OR cb.name LIKE ?)
               ORDER BY i.id DESC""",
            (q, "%%%s%%" % q, "%%%s%%" % q, "%%%s%%" % q, "%%%s%%" % q, "%%%s%%" % q)).fetchall()
        c.close()
        return {"q": q, "results": [dict(r) for r in rows]}

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._json_body()
        if path == "/api/items":
            return self._add_item(body)
        if path == "/api/types":
            return self._add_type(body)
        if path == "/api/cabinets":
            return self._add_cabinet(body)
        if path == "/api/cats":
            return self._add_cat(body)
        return self._send(404, {"error": "not found"})

    # ---------- 物品库 ----------
    def _add_type(self, b):
        name = (b.get("name") or "").strip()[:40]
        if not name:
            return self._send(400, {"error": "物品名称不能为空"})
        cat_in = (b.get("category") or "").strip()[:10]
        who = self._who(b)
        with _db_lock:
            c = conn()
            if c.execute("SELECT id FROM types WHERE name=?", (name,)).fetchone():
                c.close()
                return self._send(400, {"error": "物品库里已经有「%s」了" % name})
            cat = ensure_cat(c, cat_in)
            c.execute("INSERT INTO types(name, category, sort) VALUES(?,?,0)", (name, cat))
            log(c, who, "物品库", "新增物品「%s」（%s）" % (name, cat))
            c.commit()
            c.close()
        return self._send(200, get_state())

    def _update_type(self, tid, b):
        who = self._who(b)
        new_name = (b.get("name") or "").strip()[:40] if "name" in b else None
        cat_in = (b.get("category") or "").strip()[:10] if "category" in b else None
        if not new_name and cat_in is None:
            return self._send(400, {"error": "没有要修改的内容"})
        with _db_lock:
            c = conn()
            row = c.execute("SELECT * FROM types WHERE id=?", (tid,)).fetchone()
            if not row:
                c.close()
                return self._send(404, {"error": "物品不存在"})
            if new_name:
                dup = c.execute("SELECT id FROM types WHERE name=? AND id<>?", (new_name, tid)).fetchone()
                if dup:
                    c.close()
                    return self._send(400, {"error": "物品库里已经有「%s」了" % new_name})
                c.execute("UPDATE types SET name=? WHERE id=?", (new_name, tid))
                c.execute("UPDATE items SET name=? WHERE type_id=?", (new_name, tid))
            if cat_in is not None:
                cat = ensure_cat(c, cat_in)
                c.execute("UPDATE types SET category=? WHERE id=?", (cat, tid))
                c.execute("UPDATE items SET category=? WHERE type_id=?", (cat, tid))
            log(c, who, "物品库", "修改物品「%s」" % (new_name or row["name"]))
            c.commit()
            c.close()
        return self._send(200, get_state())

    def _delete_type(self, tid, who):
        with _db_lock:
            c = conn()
            row = c.execute("SELECT * FROM types WHERE id=?", (tid,)).fetchone()
            if not row:
                c.close()
                return self._send(404, {"error": "物品不存在"})
            used = c.execute("SELECT COUNT(*) n, COALESCE(SUM(qty),0) q FROM items WHERE type_id=?",
                             (tid,)).fetchone()
            if used["n"]:
                c.close()
                return self._send(400, {"error": "「%s」还在 %d 个柜子里，先把它从柜子里取出再删"
                                        % (row["name"], used["n"])})
            c.execute("DELETE FROM types WHERE id=?", (tid,))
            log(c, who, "物品库", "删除物品「%s」" % row["name"])
            c.commit()
            c.close()
        return self._send(200, get_state())

    # ---------- 物品「种类」（可自定义） ----------
    def _add_cat(self, b):
        name = (b.get("name") or "").strip()[:10]
        if not name:
            return self._send(400, {"error": "种类名称不能为空"})
        who = self._who(b)
        with _db_lock:
            c = conn()
            if name in cat_names(c):
                c.close()
                return self._send(400, {"error": "已经有「%s」这个种类了" % name})
            c.execute("INSERT INTO cats(name, sort) SELECT ?, COALESCE(MAX(sort),0)+1 FROM cats", (name,))
            log(c, who, "种类", "新增种类「%s」" % name)
            c.commit()
            c.close()
        return self._send(200, get_state())

    def _update_cat(self, cid, b):
        name = (b.get("name") or "").strip()[:10]
        if not name:
            return self._send(400, {"error": "种类名称不能为空"})
        who = self._who(b)
        with _db_lock:
            c = conn()
            row = c.execute("SELECT * FROM cats WHERE id=?", (cid,)).fetchone()
            if not row:
                c.close()
                return self._send(404, {"error": "种类不存在"})
            if name != row["name"] and name in cat_names(c):
                c.close()
                return self._send(400, {"error": "已经有「%s」这个种类了" % name})
            c.execute("UPDATE cats SET name=? WHERE id=?", (name, cid))
            c.execute("UPDATE types SET category=? WHERE category=?", (name, row["name"]))
            c.execute("UPDATE items SET category=? WHERE category=?", (name, row["name"]))
            log(c, who, "种类", "种类「%s」改名为「%s」" % (row["name"], name))
            c.commit()
            c.close()
        return self._send(200, get_state())

    def _delete_cat(self, cid, who):
        with _db_lock:
            c = conn()
            row = c.execute("SELECT * FROM cats WHERE id=?", (cid,)).fetchone()
            if not row:
                c.close()
                return self._send(404, {"error": "种类不存在"})
            total = c.execute("SELECT COUNT(*) n FROM cats").fetchone()["n"]
            if total <= 1:
                c.close()
                return self._send(400, {"error": "至少要留一个种类"})
            used = c.execute("SELECT COUNT(*) n FROM types WHERE category=?", (row["name"],)).fetchone()["n"]
            if used:
                c.close()
                return self._send(400, {"error": "还有 %d 种物品属于「%s」，先把它们改成别的种类"
                                        % (used, row["name"])})
            c.execute("DELETE FROM cats WHERE id=?", (cid,))
            log(c, who, "种类", "删除种类「%s」" % row["name"])
            c.commit()
            c.close()
        return self._send(200, get_state())

    # ---------- 柜内物品 ----------
    def _add_item(self, b):
        try:
            cab_id = int(b.get("cabinetId"))
        except Exception:
            return self._send(400, {"error": "缺少柜子"})
        try:
            qty = max(1, min(999, int(b.get("qty") or 1)))
        except Exception:
            qty = 1
        note = (b.get("note") or "").strip()[:120]
        who = self._who(b)
        ts = now_str()
        with _db_lock:
            c = conn()
            cab = c.execute("SELECT cb.code, r.name FROM cabinets cb JOIN rooms r ON r.id=cb.room_id "
                            "WHERE cb.id=?", (cab_id,)).fetchone()
            if not cab:
                c.close()
                return self._send(404, {"error": "柜子不存在"})
            # 优先按物品库 ID，其次按名称（没有就自动建一条基础数据）
            typ = None
            if b.get("typeId"):
                typ = c.execute("SELECT * FROM types WHERE id=?", (int(b["typeId"]),)).fetchone()
            if not typ:
                name = (b.get("name") or "").strip()[:40]
                if not name:
                    c.close()
                    return self._send(400, {"error": "先选一个物品"})
                typ = c.execute("SELECT * FROM types WHERE name=?", (name,)).fetchone()
                if not typ:
                    cat = b.get("category") if b.get("category") in CATEGORIES else "其他"
                    c.execute("INSERT INTO types(name, category, sort) VALUES(?,?,0)", (name, cat))
                    typ = c.execute("SELECT * FROM types WHERE id=last_insert_rowid()").fetchone()
            exist = c.execute("SELECT * FROM items WHERE cabinet_id=? AND type_id=?",
                              (cab_id, typ["id"])).fetchone()
            if exist:
                newqty = min(999, exist["qty"] + qty)
                c.execute("UPDATE items SET qty=?, note=?, updated_at=? WHERE id=?",
                          (newqty, note or exist["note"], ts, exist["id"]))
                log(c, who, "添加", "%s %s 的「%s」加到 %d 件" % (cab["name"], cab["code"], typ["name"], newqty))
            else:
                c.execute(
                    "INSERT INTO items(cabinet_id,type_id,name,category,qty,note,who,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (cab_id, typ["id"], typ["name"], typ["category"], qty, note, who, ts, ts))
                log(c, who, "添加", "%s %s 放入「%s」×%d" % (cab["name"], cab["code"], typ["name"], qty))
            c.commit()
            c.close()
        return self._send(200, get_state())

    def _update_item(self, item_id, b):
        who = self._who(b)
        sets, args = [], []
        if "typeId" in b:
            try:
                tid = int(b["typeId"])
            except Exception:
                tid = 0
            if tid:
                sets.append("type_id=?")
                args.append(tid)
        if "qty" in b:
            try:
                sets.append("qty=?")
                args.append(max(1, min(999, int(b["qty"]))))
            except Exception:
                pass
        if "note" in b:
            sets.append("note=?")
            args.append((b.get("note") or "").strip()[:120])
        if not sets:
            return self._send(400, {"error": "没有要修改的字段"})
        sets.append("updated_at=?")
        args.append(now_str())
        args.append(item_id)
        with _db_lock:
            c = conn()
            row = c.execute("SELECT i.*, t.name AS tname, cb.code, r.name AS rn FROM items i "
                            "JOIN cabinets cb ON cb.id=i.cabinet_id JOIN rooms r ON r.id=cb.room_id "
                            "LEFT JOIN types t ON t.id=i.type_id WHERE i.id=?", (item_id,)).fetchone()
            if not row:
                c.close()
                return self._send(404, {"error": "柜子里没有这件物品"})
            c.execute("UPDATE items SET %s WHERE id=?" % ",".join(sets), args)
            log(c, who, "修改", "%s %s 的「%s」" % (row["rn"], row["code"], row["tname"] or row["name"]))
            c.commit()
            c.close()
        return self._send(200, get_state())

    def _delete_item(self, item_id, who):
        with _db_lock:
            c = conn()
            row = c.execute("SELECT i.*, t.name AS tname, cb.code, r.name AS rn FROM items i "
                            "JOIN cabinets cb ON cb.id=i.cabinet_id JOIN rooms r ON r.id=cb.room_id "
                            "LEFT JOIN types t ON t.id=i.type_id WHERE i.id=?", (item_id,)).fetchone()
            if not row:
                c.close()
                return self._send(404, {"error": "柜子里没有这件物品"})
            c.execute("DELETE FROM items WHERE id=?", (item_id,))
            log(c, who, "取出", "%s %s 的「%s」×%d 已移出"
                % (row["rn"], row["code"], row["tname"] or row["name"], row["qty"]))
            c.commit()
            c.close()
        return self._send(200, get_state())

    # ---------- 柜子 ----------
    def _add_cabinet(self, b):
        try:
            room_id = int(b.get("roomId"))
        except Exception:
            return self._send(400, {"error": "缺少房间"})
        who = self._who(b)
        with _db_lock:
            c = conn()
            room = c.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
            if not room:
                c.close()
                return self._send(404, {"error": "房间不存在"})
            idx = c.execute("SELECT COUNT(*) n FROM rooms WHERE sort<? OR (sort=? AND id<=?)",
                            (room["sort"], room["sort"], room_id)).fetchone()["n"] - 1
            prefix = ROOM_PREFIX[idx] if 0 <= idx < len(ROOM_PREFIX) else str(idx + 1)
            codes = [r["code"] for r in c.execute("SELECT code FROM cabinets WHERE room_id=?", (room_id,))]
            nums = []
            for cd in codes:
                m = re.match(r"^%s(\d+)$" % re.escape(prefix), cd)
                if m:
                    nums.append(int(m.group(1)))
            nxt = (max(nums) + 1) if nums else 1
            code = "%s%d" % (prefix, nxt)
            name = (b.get("name") or "").strip()[:20]
            c.execute("INSERT INTO cabinets(room_id, code, name, sort) VALUES(?,?,?,?)",
                      (room_id, code, name, nxt))
            log(c, who, "柜子", "%s 新增柜子 %s" % (room["name"], code))
            c.commit()
            c.close()
        return self._send(200, get_state())

    def _update_cabinet(self, cab_id, b):
        who = self._who(b)
        sets, args = [], []
        if "name" in b:
            sets.append("name=?")
            args.append((b.get("name") or "").strip()[:20])
        if "code" in b and (b.get("code") or "").strip():
            code = b["code"].strip().upper()[:6]
            sets.append("code=?")
            args.append(code)
        if not sets:
            return self._send(400, {"error": "没有要修改的内容"})
        with _db_lock:
            c = conn()
            cab = c.execute("SELECT * FROM cabinets WHERE id=?", (cab_id,)).fetchone()
            if not cab:
                c.close()
                return self._send(404, {"error": "柜子不存在"})
            if "code" in b:
                dup = c.execute("SELECT id FROM cabinets WHERE code=? AND id<>?", (args[-1], cab_id)).fetchone()
                if dup:
                    c.close()
                    return self._send(400, {"error": "编号 %s 已经被别的柜子用了" % args[-1]})
            c.execute("UPDATE cabinets SET %s WHERE id=?" % ",".join(sets), args + [cab_id])
            detail = "柜子 %s 改成 %s%s" % (cab["code"], (b.get("code") or cab["code"]),
                                            ("（%s）" % b["name"]) if b.get("name") else "")
            log(c, who, "柜子", detail)
            c.commit()
            c.close()
        return self._send(200, get_state())

    def _delete_cabinet(self, cab_id, who):
        with _db_lock:
            c = conn()
            cab = c.execute("SELECT cb.*, r.name AS rn FROM cabinets cb JOIN rooms r ON r.id=cb.room_id "
                            "WHERE cb.id=?", (cab_id,)).fetchone()
            if not cab:
                c.close()
                return self._send(404, {"error": "柜子不存在"})
            n = c.execute("SELECT COUNT(*) n FROM items WHERE cabinet_id=?", (cab_id,)).fetchone()["n"]
            if n:
                c.close()
                return self._send(400, {"error": "柜子里还有 %d 种物品，先清空再删柜子" % n})
            c.execute("DELETE FROM cabinets WHERE id=?", (cab_id,))
            log(c, who, "柜子", "删除 %s %s 柜子" % (cab["rn"], cab["code"]))
            c.commit()
            c.close()
        return self._send(200, get_state())

    def _update_room(self, room_id, b):
        name = (b.get("name") or "").strip()[:20]
        who = self._who(b)
        if not name:
            return self._send(400, {"error": "房间名不能为空"})
        with _db_lock:
            c = conn()
            room = c.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
            if not room:
                c.close()
                return self._send(404, {"error": "房间不存在"})
            c.execute("UPDATE rooms SET name=? WHERE id=?", (name, room_id))
            log(c, who, "房间", "房间「%s」改名为「%s」" % (room["name"], name))
            c.commit()
            c.close()
        return self._send(200, get_state())

    def do_PATCH(self):
        path = urlparse(self.path).path
        body = self._json_body()
        m = re.match(r"^/api/items/(\d+)$", path)
        if m:
            return self._update_item(int(m.group(1)), body)
        m = re.match(r"^/api/types/(\d+)$", path)
        if m:
            return self._update_type(int(m.group(1)), body)
        m = re.match(r"^/api/cabinets/(\d+)$", path)
        if m:
            return self._update_cabinet(int(m.group(1)), body)
        m = re.match(r"^/api/rooms/(\d+)$", path)
        if m:
            return self._update_room(int(m.group(1)), body)
        m = re.match(r"^/api/cats/(\d+)$", path)
        if m:
            return self._update_cat(int(m.group(1)), body)
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        who = (q.get("who") or [""])[0].strip()[:20] or "家人"
        m = re.match(r"^/api/items/(\d+)$", path)
        if m:
            return self._delete_item(int(m.group(1)), who)
        m = re.match(r"^/api/types/(\d+)$", path)
        if m:
            return self._delete_type(int(m.group(1)), who)
        m = re.match(r"^/api/cabinets/(\d+)$", path)
        if m:
            return self._delete_cabinet(int(m.group(1)), who)
        m = re.match(r"^/api/cats/(\d+)$", path)
        if m:
            return self._delete_cat(int(m.group(1)), who)
        return self._send(404, {"error": "not found"})


def main():
    init_db()
    port = int(os.environ.get("PORT") or 3000)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("family storage running on 0.0.0.0:%d" % port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
