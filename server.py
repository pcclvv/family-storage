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
import time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
DATA = os.environ.get("STORAGE_DIR") or os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA, "storage.db")

CATEGORIES = ["被子", "床单", "被套", "枕头", "毛毯", "其他"]   # 初始种类，之后可在页面上自己加
ROOM_PREFIX = ["A", "B", "C", "D", "E", "F", "G", "H"]
FLOORS = ["F2", "F3", "F4"]     # 楼层：每层房间与柜子结构相同，柜内物品各自独立
DEFAULT_FLOOR = "F3"            # 首页默认进 F3
PAGE_VER = "v1160"   # 页面版本号，前端拿它对比，不一致就强制重载（防手机缓存旧页面）
_db_lock = threading.Lock()


BAK_PATH = DB_PATH + ".bak"
START_TS = time.time()


def backup_db():
    """每次写操作后把数据库整份备份一份，防止文件被重置/损坏时数据全丢"""
    try:
        src = sqlite3.connect(DB_PATH, timeout=10)
        dst = sqlite3.connect(BAK_PATH)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
    except Exception:
        pass


def restore_if_missing():
    """数据库文件不见了（例如环境重启）但备份还在，就自动还原"""
    if not os.path.exists(DB_PATH) and os.path.exists(BAK_PATH):
        try:
            with open(BAK_PATH, "rb") as f, open(DB_PATH, "wb") as g:
                g.write(f.read())
            return True
        except Exception:
            return False
    return False


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    os.makedirs(DATA, exist_ok=True)
    restore_if_missing()
    fresh = not os.path.exists(DB_PATH)
    c = conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS rooms (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          sort INTEGER NOT NULL DEFAULT 0,
          floor TEXT NOT NULL DEFAULT 'F3'
        );
        CREATE TABLE IF NOT EXISTS cabinets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          room_id INTEGER NOT NULL,
          code TEXT NOT NULL,
          name TEXT NOT NULL DEFAULT '',
          size TEXT NOT NULL DEFAULT 'full',
          sort INTEGER NOT NULL DEFAULT 0,
          floor TEXT NOT NULL DEFAULT 'F3'
        );
        CREATE TABLE IF NOT EXISTS types (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          category TEXT NOT NULL DEFAULT '其他',
          sort INTEGER NOT NULL DEFAULT 0,
          floor TEXT NOT NULL DEFAULT 'F3',
          inuse INTEGER NOT NULL DEFAULT 0,
          note TEXT NOT NULL DEFAULT ''
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
        CREATE TABLE IF NOT EXISTS config (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_items_cab ON items(cabinet_id);
        """
    )
    migrate(c)
    merge_duplicate_types(c)
    normalize_single(c)
    ensure_cats(c)
    c.execute("CREATE INDEX IF NOT EXISTS idx_items_type ON items(type_id)")
    if fresh or c.execute("SELECT COUNT(*) n FROM rooms").fetchone()["n"] == 0:
        seed(c)
    ensure_floors(c)
    c.commit()
    c.close()


def merge_duplicate_types(c):
    """一种物品只应该待在一个柜子里；历史上重复放到多个柜子的，合并成一条"""
    rows = c.execute(
        "SELECT i.type_id AS type_id, r.floor AS floor, COUNT(*) n FROM items i "
        "JOIN cabinets cb ON cb.id=i.cabinet_id JOIN rooms r ON r.id=cb.room_id "
        "WHERE i.type_id>0 GROUP BY i.type_id, r.floor HAVING n>1").fetchall()
    for row in rows:
        items = c.execute(
            "SELECT i.*, cb.code, r.name AS rn, r.floor AS floor FROM items i "
            "JOIN cabinets cb ON cb.id=i.cabinet_id JOIN rooms r ON r.id=cb.room_id "
            "WHERE i.type_id=? AND r.floor=? ORDER BY i.id",
            (row["type_id"], row["floor"])).fetchall()
        keep = items[0]
        for it in items[1:]:
            c.execute("DELETE FROM items WHERE id=?", (it["id"],))
        log(c, "系统", "整理", "「%s」在 %s 原来放在 %d 个柜子（%s），已合并到 %s %s"
            % (keep["name"], keep["floor"], len(items),
               "、".join("%s %s" % (i["rn"], i["code"]) for i in items),
               keep["rn"], keep["code"]))


def normalize_single(c):
    """物品原则上只有一件：历史遗留的多件数量统一归一（一件物品 = 一条记录）"""
    n1 = c.execute("UPDATE items SET qty=1 WHERE qty<>1").rowcount
    n2 = c.execute("UPDATE types SET inuse=1 WHERE inuse>1").rowcount
    if n1 or n2:
        log(c, "系统", "整理", "按「一种物品一件」归一：%d 条柜内数量、%d 条在用数量" % (n1, n2))


def migrate(c):
    """旧版本（物品自带名称）升级为「物品基础库 + 柜内引用」"""
    cb_cols = [r["name"] for r in c.execute("PRAGMA table_info(cabinets)")]
    if "size" not in cb_cols:                       # 柜子大小：full 占一格 / half 占半格
        c.execute("ALTER TABLE cabinets ADD COLUMN size TEXT NOT NULL DEFAULT 'full'")
    cols = [r["name"] for r in c.execute("PRAGMA table_info(items)")]
    if "type_id" not in cols:
        c.execute("ALTER TABLE items ADD COLUMN type_id INTEGER NOT NULL DEFAULT 0")
    # 楼层：老数据默认都在 F3，其余楼层按 F3 的结构自动镜像出来（物品各自独立）
    rm_cols = [r["name"] for r in c.execute("PRAGMA table_info(rooms)")]
    if "floor" not in rm_cols:
        c.execute("ALTER TABLE rooms ADD COLUMN floor TEXT NOT NULL DEFAULT 'F3'")
    cb_cols2 = [r["name"] for r in c.execute("PRAGMA table_info(cabinets)")]
    if "floor" not in cb_cols2:
        c.execute("ALTER TABLE cabinets ADD COLUMN floor TEXT NOT NULL DEFAULT 'F3'")
    # 物品库也按楼层分开：老物品库都归 F3，其余楼层各复制一份自己的
    tp_cols = [r["name"] for r in c.execute("PRAGMA table_info(types)")]
    if "floor" not in tp_cols:
        c.execute("ALTER TABLE types ADD COLUMN floor TEXT NOT NULL DEFAULT 'F3'")
    if "inuse" not in tp_cols:      # 使用中：没放进柜子，但在家里用着（如冬天床上盖着的被子）
        c.execute("ALTER TABLE types ADD COLUMN inuse INTEGER NOT NULL DEFAULT 0")
    if "note" not in tp_cols:       # 备注：点开物品就能看/改（如「冬天盖的厚被子，放主卧」）
        c.execute("ALTER TABLE types ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    ensure_floors(c)
    unknown = c.execute("SELECT * FROM items WHERE type_id=0").fetchall()
    for it in unknown:
        name = (it["name"] or "").strip() or "未命名物品"
        cat = it["category"] if it["category"] in CATEGORIES else "其他"
        it_floor = c.execute(
            "SELECT COALESCE(NULLIF(r.floor,''),'F3') AS floor FROM cabinets cb "
            "JOIN rooms r ON r.id=cb.room_id WHERE cb.id=?", (it["cabinet_id"],)).fetchone()
        it_floor = (it_floor["floor"] if it_floor else None) or DEFAULT_FLOOR
        row = c.execute("SELECT id FROM types WHERE name=? AND COALESCE(NULLIF(floor,''),'F3')=?",
                        (name, it_floor)).fetchone()
        if row:
            tid = row["id"]
        else:
            c.execute("INSERT INTO types(name,category,sort,floor) VALUES(?,?,?,?)",
                      (name, cat, 0, it_floor))
            tid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        c.execute("UPDATE items SET type_id=? WHERE id=?", (tid, it["id"]))


def ensure_floors(c):
    """楼层结构：F2 / F3 / F4 每层的房间、柜子（数量/编号/大小/排序）完全一致。
       物品库与柜内物品各层独立，新楼层从空白开始，不复制别的层"""
    have = {r["floor"] or DEFAULT_FLOOR for r in c.execute("SELECT DISTINCT floor FROM rooms")}
    if not have:
        return
    tpl = DEFAULT_FLOOR if DEFAULT_FLOOR in have else sorted(have)[0]
    rooms = [dict(r) for r in c.execute("SELECT * FROM rooms WHERE floor=? ORDER BY sort, id", (tpl,))]
    for fl in FLOORS:
        if fl in have:
            continue
        for rm in rooms:
            c.execute("INSERT INTO rooms(name, sort, floor) VALUES(?,?,?)",
                      (rm["name"], rm["sort"], fl))
            new_room = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            for cab in c.execute("SELECT * FROM cabinets WHERE room_id=? ORDER BY sort, id", (rm["id"],)):
                c.execute("INSERT INTO cabinets(room_id, code, name, size, sort, floor) "
                          "VALUES(?,?,?,?,?,?)",
                          (new_room, cab["code"], cab["name"], cab["size"], cab["sort"], fl))
        log(c, "系统", "楼层", "新增楼层 %s：房间和柜子布置与 %s 一致，物品库和柜内物品各自从空开始" % (fl, tpl))


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
        c.execute("INSERT INTO rooms(name, sort, floor) VALUES(?,?,?)", (name, sort, DEFAULT_FLOOR))
    rooms = [dict(r) for r in c.execute("SELECT * FROM rooms ORDER BY sort, id")]
    cab_ids = {}
    for idx, room in enumerate(rooms):
        prefix = ROOM_PREFIX[idx] if idx < len(ROOM_PREFIX) else str(idx + 1)
        for i in range(1, 6):
            code = "%s%d" % (prefix, i)
            c.execute("INSERT INTO cabinets(room_id, code, name, size, sort, floor) VALUES(?,?,?,?,?,?)",
                      (room["id"], code, "", "half" if i == 1 else "full", i, DEFAULT_FLOOR))
            cab_ids[code] = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]

    lib = [("冬被", "被子"), ("夏被", "被子"), ("蚕丝被", "被子"), ("四件套", "被套"),
           ("床单", "床单"), ("被套", "被套"), ("枕头", "枕头"), ("毛毯", "毛毯")]
    tid = {}
    for i, (name, cat) in enumerate(lib):
        c.execute("INSERT INTO types(name, category, sort) VALUES(?,?,?)", (name, cat, i))
        tid[name] = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]

    ts = now_str()
    demo = [
        ("A1", "冬被", "厚羽绒，压缩袋装"),
        ("A1", "四件套", "灰色棉麻"),
        ("A2", "夏被", "薄空调被"),
        ("A3", "蚕丝被", ""),
        ("A4", "床单", "1.8m 床单"),
        ("B2", "枕头", "乳胶枕"),
        ("B2", "毛毯", "珊瑚绒"),
        ("B5", "被套", "备用"),
    ]
    for code, name, note in demo:
        cat = dict(lib)[name]
        c.execute(
            "INSERT INTO items(cabinet_id,type_id,name,category,qty,note,who,created_at,updated_at) "
            "VALUES(?,?,?,?,1,?,?,?,?)",
            (cab_ids[code], tid[name], name, cat, note, "示例", ts, ts))
    c.execute("INSERT INTO logs(who,action,detail,at) VALUES(?,?,?,?)",
              ("系统", "初始化", "已创建 主卧/次卧 各 5 个柜子，物品库预置 8 种常见床品", ts))


def log(c, who, action, detail):
    c.execute("INSERT INTO logs(who,action,detail,at) VALUES(?,?,?,?)",
              (who or "家人", action, detail, now_str()))


def _cab_sort_key(code):
    """柜子编号自然排序：字母前缀 + 数字大小（A1 A2 … A10 在 A2 后面）"""
    cd = (code or "").strip()
    m = re.match(r"^([A-Za-z]+)(\d+)$", cd)
    if m:
        return (0, m.group(1).upper(), int(m.group(2)), cd)
    return (1, cd.upper(), 0, cd)


def get_state():
    c = conn()
    rooms = [dict(r) for r in c.execute("SELECT * FROM rooms ORDER BY sort, id")]
    # 柜子按编号自然排序（字母前缀 + 数字），改名/删了再补也归位
    cabs = [dict(r) for r in c.execute("SELECT * FROM cabinets ORDER BY room_id, id")]
    cabs.sort(key=lambda r: (_cab_sort_key(r["code"]), r["id"]))
    items = [dict(r) for r in c.execute(
        "SELECT i.*, t.name AS tname, t.category AS tcat FROM items i "
        "LEFT JOIN types t ON t.id=i.type_id ORDER BY i.id DESC")]
    types = [dict(r) for r in c.execute("SELECT * FROM types ORDER BY sort, id")]
    cats_rows = [dict(r) for r in c.execute("SELECT * FROM cats ORDER BY sort, id")]
    cats = [r["name"] for r in cats_rows]
    logs = [dict(r) for r in c.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 12")]
    cfg = {r["key"]: r["value"] for r in c.execute("SELECT * FROM config")}
    for it in items:
        if it.get("tname"):
            it["category"] = it["tcat"] or it["category"]
            # 柜子底部手动新增时可自定义名字：只有没改过名的才跟着类型名走
            if not it["name"] or it["name"] == it["tname"]:
                it["name"] = it["tname"]
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
    places = {}   # 物品 -> {楼层: 位置}，每层各放各的，互不影响
    for room in rooms:
        fl = room.get("floor") or DEFAULT_FLOOR
        room["floor"] = fl
        room["cabinets"] = [x for x in cabs if x["room_id"] == room["id"]]
        room["count"] = sum(x["count"] for x in room["cabinets"])
        for cab in room["cabinets"]:
            cab["floor"] = fl
            for it in cab["items"]:
                places.setdefault(it["type_id"], {})[fl] = {
                    "room": room["name"], "code": cab["code"],
                    "cabinet_id": cab["id"], "qty": it["qty"], "floor": fl,
                    "label": "%s %s %s" % (fl, room["name"], cab["code"])}
    for t in types:
        t["floor"] = t.get("floor") or DEFAULT_FLOOR
        t["inuse"] = int(t.get("inuse") or 0)
        t["places"] = places.get(t["id"], {})
        t["place"] = t["places"].get(DEFAULT_FLOOR) or next(iter(t["places"].values()), None)
    return {"rooms": rooms, "logs": logs, "types": types, "categories": cats, "cats": cats_rows,
            "cfg": cfg, "ver": PAGE_VER, "floors": FLOORS, "defaultFloor": DEFAULT_FLOOR}


TABLE_COLS = {
    "rooms": {"id", "name", "sort", "floor"},
    "cabinets": {"id", "room_id", "code", "name", "size", "sort", "floor"},
    "types": {"id", "name", "category", "sort", "floor", "inuse", "note"},
    "cats": {"id", "name", "sort"},
    "items": {"id", "cabinet_id", "type_id", "name", "category", "qty", "note", "who",
              "created_at", "updated_at"},
    "logs": {"id", "who", "action", "detail", "at"},
    "config": {"key", "value"},
}


def dump_all():
    """导出全部数据（备份用）"""
    c = conn()
    data = {t: [dict(r) for r in c.execute("SELECT * FROM %s" % t)] for t in TABLE_COLS}
    c.close()
    return {"exported_at": now_str(), "data": data}


def load_all(payload):
    """整份导入，覆盖现有数据（文件不对就拒绝，别把现有数据冲掉）"""
    data = (payload or {}).get("data") or {}
    if not data.get("rooms") or not data.get("cabinets"):
        return {"error": "这个备份文件里没有房间/柜子数据，没有导入"}
    with _db_lock:
        c = conn()
        for t in ("items", "cabinets", "types", "cats", "rooms", "logs", "config"):
            c.execute("DELETE FROM %s" % t)
        for t in ("rooms", "cabinets", "types", "cats", "items", "logs", "config"):
            cols = TABLE_COLS[t]
            for row in data.get(t, []):
                keys = [k for k in row.keys() if k in cols]
                if not keys:
                    continue
                c.execute("INSERT INTO %s(%s) VALUES(%s)" % (t, ",".join(keys), ",".join("?" * len(keys))),
                          [row[k] for k in keys])
        ensure_cats(c)
        ensure_floors(c)
        log(c, "导入", "导入", "覆盖导入了 %d 条柜内物品记录" % len(data.get("items", [])))
        c.commit()
        c.close()
    return get_state()


class Handler(BaseHTTPRequestHandler):
    server_version = "StorageHome/2.0"

    # ---------- 工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if code == 200 and getattr(self, "command", "") in ("POST", "PATCH", "DELETE"):
            backup_db()
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
            return self._send(200, {
                "ok": True, "time": now_str(), "pid": os.getpid(),
                "uptime": int(time.time() - START_TS),
                "db": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
                "bak": os.path.getsize(BAK_PATH) if os.path.exists(BAK_PATH) else 0,
            })
        if path == "/api/export":
            return self._send(200, dump_all())
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
                      cb.code, cb.name AS cab_name, r.name AS room_name,
                      COALESCE(NULLIF(r.floor,''),'F3') AS floor
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
        if path == "/api/config":
            return self._set_config(body)
        if path == "/api/import":
            res = load_all(body)
            if isinstance(res, dict) and res.get("error"):
                return self._send(400, res)
            return self._send(200, res)
        return self._send(404, {"error": "not found"})

    # ---------- 物品库（按楼层分开） ----------
    def _add_type(self, b):
        name = (b.get("name") or "").strip()[:40]
        if not name:
            return self._send(400, {"error": "物品名称不能为空"})
        cat_in = (b.get("category") or "").strip()[:10]
        note_in = (b.get("note") or "").strip()[:120]
        fl = (b.get("floor") or "").strip().upper()
        if not fl:
            fl = DEFAULT_FLOOR
        elif fl not in FLOORS:
            return self._send(400, {"error": "楼层不对，只能是 " + "/".join(FLOORS)})
        who = self._who(b)
        with _db_lock:
            c = conn()
            if c.execute("SELECT id FROM types WHERE name=? AND COALESCE(NULLIF(floor,''),'F3')=?",
                         (name, fl)).fetchone():
                c.close()
                return self._send(400, {"error": "%s 的物品库里已经有「%s」了" % (fl, name)})
            cat = ensure_cat(c, cat_in)
            c.execute("INSERT INTO types(name, category, sort, floor, note) VALUES(?,?,0,?,?)",
                      (name, cat, fl, note_in))
            log(c, who, "物品库", "%s 新增物品「%s」（%s）" % (fl, name, cat))
            c.commit()
            c.close()
        return self._send(200, get_state())

    def _update_type(self, tid, b):
        who = self._who(b)
        new_name = (b.get("name") or "").strip()[:40] if "name" in b else None
        cat_in = (b.get("category") or "").strip()[:10] if "category" in b else None
        inuse_set = None
        if "inuse" in b:
            try:
                # 「在用」只是个勾选：勾上＝在外面用（1），不勾＝不在用（0）
                inuse_set = 1 if int(b.get("inuse") or 0) > 0 else 0
            except Exception:
                inuse_set = None
        note_set = (b.get("note") or "").strip()[:120] if "note" in b else None
        if not new_name and cat_in is None and inuse_set is None and note_set is None:
            return self._send(400, {"error": "没有要修改的内容"})
        with _db_lock:
            c = conn()
            row = c.execute("SELECT * FROM types WHERE id=?", (tid,)).fetchone()
            if not row:
                c.close()
                return self._send(404, {"error": "物品不存在"})
            fl = row["floor"] or DEFAULT_FLOOR
            if new_name:
                dup = c.execute(
                    "SELECT id FROM types WHERE name=? AND id<>? AND COALESCE(NULLIF(floor,''),'F3')=?",
                    (new_name, tid, fl)).fetchone()
                if dup:
                    c.close()
                    return self._send(400, {"error": "%s 的物品库里已经有「%s」了" % (fl, new_name)})
                c.execute("UPDATE types SET name=? WHERE id=?", (new_name, tid))
                # 只同步没改过名的物品，手动起的名字保持不变
                c.execute("UPDATE items SET name=? WHERE type_id=? AND name=?",
                          (new_name, tid, row["name"]))
            if cat_in is not None:
                cat = ensure_cat(c, cat_in)
                c.execute("UPDATE types SET category=? WHERE id=?", (cat, tid))
                c.execute("UPDATE items SET category=? WHERE type_id=?", (cat, tid))
            extra = ""
            if inuse_set is not None:
                if inuse_set > 0:
                    # 勾成「在用」＝东西已经拿出来用了：直接从本层柜子里取出，
                    # 取出的件数记到「在用」上，总数不会凭空变少
                    got = c.execute(
                        "SELECT COALESCE(SUM(i.qty),0) FROM items i "
                        "JOIN cabinets cb ON cb.id=i.cabinet_id "
                        "JOIN rooms r ON r.id=cb.room_id "
                        "WHERE i.type_id=? AND COALESCE(NULLIF(r.floor,''),'F3')=?",
                        (tid, fl)).fetchone()[0] or 0
                    if got > 0:
                        c.execute(
                            "DELETE FROM items WHERE type_id=? AND cabinet_id IN "
                            "(SELECT cb.id FROM cabinets cb JOIN rooms r ON r.id=cb.room_id "
                            " WHERE COALESCE(NULLIF(r.floor,''),'F3')=?)", (tid, fl))
                        inuse_set = 1
                        extra = "，已从柜子取出记为在用"
                c.execute("UPDATE types SET inuse=? WHERE id=?", (inuse_set, tid))
            if note_set is not None:
                c.execute("UPDATE types SET note=? WHERE id=?", (note_set, tid))
            log(c, who, "物品库", "修改 %s 的物品「%s」%s" % (fl, new_name or row["name"], extra))
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
            c.execute("DELETE FROM items WHERE type_id=?", (tid,))
            c.execute("DELETE FROM types WHERE id=?", (tid,))
            detail = "删除物品「%s」" % row["name"]
            if used["n"]:
                detail += "，并从柜子里取出 %d 件" % used["q"]
            log(c, who, "物品库", detail)
            c.commit()
            c.close()
        return self._send(200, get_state())

    # ---------- 物品「种类」（可自定义） ----------
    # ---------- 全局设置 ----------
    DOOR_LABEL = {"solid": "实门", "glass": "半透明", "clear": "全透明"}

    def _set_config(self, b):
        who = self._who(b)
        allowed = ("doorGlass",)
        sets = {k: str(b.get(k) or "").strip()[:20] for k in allowed if k in b}
        sets = {k: v for k, v in sets.items() if v}
        if not sets:
            return self._send(400, {"error": "没有要保存的设置"})
        with _db_lock:
            c = conn()
            for k, v in sets.items():
                c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
                if k == "doorGlass":
                    log(c, who, "设置", "柜门玻璃改成「%s」" % self.DOOR_LABEL.get(v, v))
            c.commit()
            c.close()
        return self._send(200, get_state())

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
        # 一种物品原则上只有一件，数量固定 1
        note = (b.get("note") or "").strip()[:120]
        who = self._who(b)
        ts = now_str()
        with _db_lock:
            c = conn()
            cab = c.execute("SELECT cb.code, r.name, COALESCE(NULLIF(r.floor,''),'F3') AS floor "
                            "FROM cabinets cb JOIN rooms r ON r.id=cb.room_id "
                            "WHERE cb.id=?", (cab_id,)).fetchone()
            if not cab:
                c.close()
                return self._send(404, {"error": "柜子不存在"})
            # 优先按物品库 ID，其次按名称（没有就自动建一条基础数据）
            # 物品库按楼层分开：只能用本层的物品
            cab_floor = cab["floor"] or DEFAULT_FLOOR
            typ = None
            if b.get("typeId"):
                typ = c.execute("SELECT * FROM types WHERE id=?", (int(b["typeId"]),)).fetchone()
                if typ and (typ["floor"] or DEFAULT_FLOOR) != cab_floor:
                    c.close()
                    return self._send(400, {"error": "「%s」是 %s 的物品，%s 要用自己的物品库"
                                                     % (typ["name"], typ["floor"] or DEFAULT_FLOOR, cab_floor)})
            if not typ:
                name = (b.get("name") or "").strip()[:40]
                if not name:
                    c.close()
                    return self._send(400, {"error": "先选一个物品"})
                typ = c.execute("SELECT * FROM types WHERE name=? AND COALESCE(NULLIF(floor,''),'F3')=?",
                                (name, cab_floor)).fetchone()
                if not typ:
                    cat = ensure_cat(c, (b.get("category") or "").strip()[:10])
                    c.execute("INSERT INTO types(name, category, sort, floor) VALUES(?,?,0,?)",
                              (name, cat, cab_floor))
                    typ = c.execute("SELECT * FROM types WHERE id=last_insert_rowid()").fetchone()
            # 一种物品只能待在一个柜子里：已经放在别处就不再重复放
            other = c.execute(
                "SELECT i.id, i.qty, cb.code, r.name AS rn FROM items i "
                "JOIN cabinets cb ON cb.id=i.cabinet_id JOIN rooms r ON r.id=cb.room_id "
                "WHERE i.type_id=? AND i.cabinet_id<>? AND COALESCE(NULLIF(r.floor,''),'F3')=?",
                (typ["id"], cab_id, cab["floor"])).fetchone()
            if other:
                c.close()
                return self._send(409, {"error": "本层的「%s」已经在 %s %s 里。同层一种物品只放一个柜子，"
                                                 "想换地方先在原柜子里取出"
                                                 % (typ["name"], other["rn"], other["code"])})
            exist = c.execute("SELECT * FROM items WHERE cabinet_id=? AND type_id=?",
                              (cab_id, typ["id"])).fetchone()
            # 柜子底部手动新增时可以自定义显示名（类型仍在物品库维护，只做挂靠）
            disp = (b.get("name") or "").strip()[:40] if b.get("typeId") else ""
            if disp and disp == typ["name"]:
                disp = ""
            if exist:
                # 已经在这个柜子里了：不重复加，只顺手把备注补上（一件物品只有一件）
                if note and note != exist["note"]:
                    c.execute("UPDATE items SET note=?, updated_at=? WHERE id=?",
                              (note, ts, exist["id"]))
                log(c, who, "添加", "%s %s 的「%s」已经在柜子里了" % (cab["name"], cab["code"], exist["name"]))
            else:
                item_name = disp or typ["name"]
                c.execute(
                    "INSERT INTO items(cabinet_id,type_id,name,category,qty,note,who,created_at,updated_at) "
                    "VALUES(?,?,?,?,1,?,?,?,?)",
                    (cab_id, typ["id"], item_name, typ["category"], note, who, ts, ts))
                log(c, who, "添加", "%s %s 放入「%s」" % (cab["name"], cab["code"], item_name))
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
            # 一种物品原则上只有一件：数量固定 1，不再支持多件
            sets.append("qty=?")
            args.append(1)
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
            row = c.execute("SELECT i.*, t.name AS tname, cb.code, r.name AS rn, "
                            "COALESCE(NULLIF(r.floor,''),'F3') AS floor "
                            "FROM items i "
                            "JOIN cabinets cb ON cb.id=i.cabinet_id JOIN rooms r ON r.id=cb.room_id "
                            "LEFT JOIN types t ON t.id=i.type_id WHERE i.id=?", (item_id,)).fetchone()
            if not row:
                c.close()
                return self._send(404, {"error": "柜子里没有这件物品"})
            if "typeId" in b and args and args[0] != row["type_id"]:
                tgt = c.execute("SELECT * FROM types WHERE id=?", (args[0],)).fetchone()
                if not tgt:
                    c.close()
                    return self._send(400, {"error": "这个物品在物品库里找不到了"})
                if (tgt["floor"] or DEFAULT_FLOOR) != row["floor"]:
                    c.close()
                    return self._send(400, {"error": "「%s」是 %s 的物品，%s 的柜子只能用本层物品库"
                                                     % (tgt["name"], tgt["floor"] or DEFAULT_FLOOR, row["floor"])})
                other = c.execute(
                    "SELECT i.qty, cb.code, r.name AS rn FROM items i "
                    "JOIN cabinets cb ON cb.id=i.cabinet_id JOIN rooms r ON r.id=cb.room_id "
                    "WHERE i.type_id=? AND i.id<>?", (args[0], item_id)).fetchone()
                if other:
                    c.close()
                    return self._send(409, {"error": "「%s」已经在 %s %s 里，一种物品只放一个柜子"
                                                     % (row["tname"] or row["name"], other["rn"],
                                                        other["code"])})
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
            log(c, who, "取出", "%s %s 的「%s」已移出"
                % (row["rn"], row["code"], row["tname"] or row["name"]))
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
            # 编号跟着这个房间现有的柜子走（B1、B2…就接着出 B5），而不是固定按房间顺序
            fl = room["floor"] or DEFAULT_FLOOR
            codes = [r["code"] for r in c.execute("SELECT code FROM cabinets WHERE room_id=?", (room_id,))]
            groups = {}
            for cd in codes:
                m = re.match(r"^([A-Za-z]+)(\d+)$", (cd or "").strip())
                if m:
                    groups.setdefault(m.group(1).upper(), []).append(int(m.group(2)))
            if groups:
                prefix = max(groups, key=lambda p: (len(groups[p]), p))
                nums = groups[prefix]
            else:
                prefix = ROOM_PREFIX[idx] if 0 <= idx < len(ROOM_PREFIX) else str(idx + 1)
                nums = []
            nxt = (max(nums) + 1) if nums else 1
            code = "%s%d" % (prefix, nxt)
            while c.execute("SELECT id FROM cabinets WHERE code=? AND COALESCE(NULLIF(floor,''),'F3')=?",
                            (code, fl)).fetchone():
                nxt += 1
                code = "%s%d" % (prefix, nxt)
            name = (b.get("name") or "").strip()[:20]
            size = "half" if b.get("size") == "half" else "full"
            c.execute("INSERT INTO cabinets(room_id, code, name, size, sort, floor) VALUES(?,?,?,?,?,?)",
                      (room_id, code, name, size, nxt, fl))
            log(c, who, "柜子", "%s %s 新增柜子 %s" % (fl, room["name"], code))
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
        if "size" in b:
            sets.append("size=?")
            args.append("half" if b.get("size") == "half" else "full")
        if not sets:
            return self._send(400, {"error": "没有要修改的内容"})
        with _db_lock:
            c = conn()
            cab = c.execute("SELECT * FROM cabinets WHERE id=?", (cab_id,)).fetchone()
            if not cab:
                c.close()
                return self._send(404, {"error": "柜子不存在"})
            if "code" in b:
                dup = c.execute(
                    "SELECT id FROM cabinets WHERE code=? AND id<>? "
                    "AND COALESCE(NULLIF(floor,''),'F3')=?",
                    (args[-1], cab_id, cab["floor"] or DEFAULT_FLOOR)).fetchone()
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
            got = c.execute("SELECT COALESCE(SUM(qty),0) q, COUNT(*) n FROM items WHERE cabinet_id=?",
                            (cab_id,)).fetchone()
            # 删柜子＝里面的东西拿出来用：按物品记到「在用」，总数不会凭空变少
            moved = c.execute(
                "SELECT type_id, SUM(qty) q FROM items WHERE cabinet_id=? GROUP BY type_id",
                (cab_id,)).fetchall()
            for m in moved:
                if m["type_id"]:
                    # 一件物品只有一件：记成在用（勾选），不叠加数量
                    c.execute("UPDATE types SET inuse=1 WHERE id=?", (m["type_id"],))
            c.execute("DELETE FROM items WHERE cabinet_id=?", (cab_id,))
            c.execute("DELETE FROM cabinets WHERE id=?", (cab_id,))
            detail = "删除 %s %s 柜子" % (cab["rn"], cab["code"])
            if got["n"]:
                detail += "，柜里 %d 种 %d 件记为在用" % (got["n"], got["q"])
            log(c, who, "柜子", detail)
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
