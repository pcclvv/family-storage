# 发布前从线上把最新数据整份拉下来，重建 data/storage.db，保证更新不丢用户数据
import json, os, sqlite3, sys, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DB = os.path.join(DATA, "storage.db")
HOST = "https://quiltmap.app.workbuddy.host"
CK = r"D:/WorkData/quiltmap-backup/_ck3.txt"
SCHEMA = """
CREATE TABLE rooms (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0,
  floor TEXT NOT NULL DEFAULT 'F3');
CREATE TABLE cabinets (id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL, code TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '', size TEXT NOT NULL DEFAULT 'full', sort INTEGER NOT NULL DEFAULT 0,
  floor TEXT NOT NULL DEFAULT 'F3');
CREATE TABLE types (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, category TEXT NOT NULL DEFAULT '其他',
  sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE cats (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, cabinet_id INTEGER NOT NULL, type_id INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '其他', qty INTEGER NOT NULL DEFAULT 1,
  note TEXT NOT NULL DEFAULT '', who TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, who TEXT NOT NULL DEFAULT '', action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
CREATE INDEX idx_items_cab ON items(cabinet_id);
CREATE INDEX idx_items_type ON items(type_id);
"""
ORDER = ("rooms", "cabinets", "types", "cats", "items", "logs", "config")
COLS = {
    "rooms": ["id", "name", "sort", "floor"],
    "cabinets": ["id", "room_id", "code", "name", "size", "sort", "floor"],
    "types": ["id", "name", "category", "sort"],
    "cats": ["id", "name", "sort"],
    "items": ["id", "cabinet_id", "type_id", "name", "category", "qty", "note", "who",
              "created_at", "updated_at"],
    "logs": ["id", "who", "action", "detail", "at"],
    "config": ["key", "value"],
}

cookie = ""
for line in open(CK, encoding="utf-8").read().splitlines():
    line = line.strip()
    if line.startswith("#HttpOnly_"):
        line = line[len("#HttpOnly_"):]
    elif line.startswith("#") or not line:
        continue
    f = line.split("\t")
    if len(f) >= 7:
        cookie += (f[5] + "=" + f[6] + "; ")

# 支持 --from 快照文件：从备份 json 重建数据库（数据被覆盖时用它恢复）
if len(sys.argv) >= 3 and sys.argv[1] == "--from":
    payload = json.load(open(sys.argv[2], encoding="utf-8"))
    data = payload.get("data") or payload
    stamp = payload.get("exported_at", "restore").replace(":", "").replace(" ", "-")
else:
    req = urllib.request.Request(HOST + "/api/export", headers={"Cookie": cookie.strip("; ")})
    try:
        payload = json.load(urllib.request.urlopen(req, timeout=30))
        data = payload.get("data") or {}
    except Exception as e:
        # 老版本没有 /api/export，用 /api/state 兜底
        req = urllib.request.Request(HOST + "/api/state", headers={"Cookie": cookie.strip("; ")})
        st = json.load(urllib.request.urlopen(req, timeout=30))
        data = {"rooms": [], "cabinets": [], "types": st.get("types", []),
                "cats": st.get("cats", [{"name": n} for n in st.get("categories", [])]),
                "items": [], "logs": st.get("logs", [])}
        for r in st.get("rooms", []):
            data["rooms"].append({"id": r["id"], "name": r["name"], "sort": r.get("sort", 0),
                                  "floor": r.get("floor", "F3")})
            for cb in r.get("cabinets", []):
                data["cabinets"].append({k: cb[k] for k in ("id", "room_id", "code", "name", "sort")
                                         if k in cb} | {"floor": cb.get("floor", "F3")})
                for i in cb.get("items", []):
                    data["items"].append({k: i.get(k) for k in COLS["items"]})
        payload = {"exported_at": st.get("time", "") or __import__("time").strftime("%Y-%m-%d %H:%M")}
    stamp = payload.get("exported_at", "").replace(":", "").replace(" ", "-")

with open(os.path.join(os.path.dirname(BASE), "quiltmap-backup", "live-%s.json" % stamp), "w",
          encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=1)

for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)
c = sqlite3.connect(DB)
c.executescript(SCHEMA)
for t in ORDER:
    for row in data.get(t, []):
        keys = [k for k in COLS[t] if k in row]
        if not keys:
            continue
        c.execute("INSERT INTO %s(%s) VALUES(%s)" % (t, ",".join(keys), ",".join("?" * len(keys))),
                  [row[k] for k in keys])
# 清掉自测留下的痕迹
c.execute("DELETE FROM cats WHERE name LIKE '%测试%'")
c.execute("DELETE FROM logs WHERE who IN ('自测','测试') OR detail LIKE '%测试%'")
c.commit()
print("导入完成:", {t: c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ORDER})
print("总件数:", c.execute("SELECT COALESCE(SUM(qty),0) FROM items").fetchone()[0])
c.close()
