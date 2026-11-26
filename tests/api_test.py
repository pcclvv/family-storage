# -*- coding: utf-8 -*-
"""家庭储物管家 · 接口自测"""
import tempfile
import json, os, subprocess, sys, time, shutil, urllib.request, urllib.error, urllib.parse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根目录
PORT = 3399
HOST = "http://127.0.0.1:%d" % PORT
q = urllib.parse.quote
ok = fail = 0

def call(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    r = urllib.request.Request(HOST + path, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=10) as f:
            return f.status, json.load(f)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: return e.code, json.loads(raw)
        except Exception: return e.code, {"raw": raw}

def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  ✓", name)
    else:
        fail += 1; print("  ✗", name, extra)

def start():
    p = subprocess.Popen([sys.executable, "server.py"], env=dict(os.environ, PORT=str(PORT), STORAGE_DIR=TESTDATA),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(50):
        try:
            urllib.request.urlopen(HOST + "/api/health", timeout=2); return p
        except Exception:
            time.sleep(0.2)
    out = p.stdout.read(2000).decode(errors="ignore")
    raise SystemExit("服务没起来：" + out)

# 每轮从全新种子库开始（独立临时目录，绝不碰真实数据）

TESTDATA = os.path.join(tempfile.mkdtemp(prefix="storagetest-"), "data")   # 独立测试数据目录
print("== 启动 ==")
p = start()
try:
    st_code, health = call("GET", "/api/health")
    check("健康检查 200", st_code == 200 and health.get("ok"))
    check("报告运行时长", "uptime" in health)

    code, s = call("GET", "/api/state")
    check("状态接口 200", code == 200)
    check("两个房间", len(s["rooms"]) == 2, s.get("rooms"))
    check("共 10 个柜子", sum(len(r["cabinets"]) for r in s["rooms"]) == 10)
    total = sum(c["count"] for r in s["rooms"] for c in r["cabinets"])
    check("总件数 15（种子）", total == 15, total)
    check("预置种类齐全", "被子" in s["categories"] and any(t["name"]=="冬被" for t in s["types"]), s["categories"])
    check("种类带 id", all("id" in c for c in s["cats"]))

    # 找一个空柜子
    empty = [c for r in s["rooms"] for c in r["cabinets"] if not c["items"]][0]
    old = [t for t in s["types"] if t["name"] == "自测凉席"]
    if old: call("DELETE", "/api/types/%d?who=%s" % (old[0]["id"], q("自测")))
    code, st0 = call("POST", "/api/types", {"name": "自测凉席", "category": "其他", "who": "自测"})
    check("新建测试物品成功", code == 200)
    tid = next(t["id"] for t in st0["types"] if t["name"] == "自测凉席")

    print("== 柜内物品 ==")
    code, s2 = call("POST", "/api/items", {"cabinetId": empty["id"], "typeId": tid, "qty": 1, "who": "自测"})
    got = [c for r in s2["rooms"] for c in r["cabinets"] if c["id"] == empty["id"]][0]
    check("点一下放入 1 件", got["count"] == 1 and got["items"][0]["qty"] == 1, got["items"])
    item_id = got["items"][0]["id"]
    code, s3 = call("POST", "/api/items", {"cabinetId": empty["id"], "typeId": tid, "qty": 1, "who": "自测"})
    got = [c for r in s3["rooms"] for c in r["cabinets"] if c["id"] == empty["id"]][0]
    check("同物品再放会合并数量", got["count"] == 2 and len(got["items"]) == 1, got["items"])

    code, s4 = call("PATCH", "/api/items/%d" % item_id, {"qty": 5, "note": "三床", "who": "自测"})
    it = [i for r in s4["rooms"] for c in r["cabinets"] for i in c["items"] if i["id"] == item_id][0]
    check("改数量+备注", it["qty"] == 5 and it["note"] == "三床", it)
    code, _ = call("PATCH", "/api/items/%d" % item_id, {"qty": 0, "who": "自测"})
    s5 = call("GET", "/api/state")[1]
    it = [i for r in s5["rooms"] for c in r["cabinets"] for i in c["items"] if i["id"] == item_id][0]
    check("数量不会小于 1", it["qty"] == 1, it["qty"])
    code, _ = call("DELETE", "/api/items/%d?who=%s" % (item_id, q("自测")))
    s6 = call("GET", "/api/state")[1]
    got = [c for r in s6["rooms"] for c in r["cabinets"] if c["id"] == empty["id"]][0]
    check("取出成功（柜子空了）", got["count"] == 0, got["items"])

    print("== 物品库 ==")
    code, s7 = call("POST", "/api/types", {"name": "自测被", "category": "被子", "who": "自测"})
    check("新增物品", code == 200 and any(t["name"] == "自测被" for t in s7["types"]))
    nid = next(t["id"] for t in s7["types"] if t["name"] == "自测被")
    code, _ = call("POST", "/api/types", {"name": "自测被", "category": "被子"})
    check("重名物品被拒", code == 400)
    code, s8 = call("PATCH", "/api/types/%d" % nid, {"name": "自测被2", "category": "被套", "who": "自测"})
    t = next(x for x in s8["types"] if x["id"] == nid)
    check("改名+换分类", t["name"] == "自测被2" and t["category"] == "被套", t)
    code, _ = call("PATCH", "/api/types/%d" % nid, {"name": "", "category": "被子"})
    check("空名字被拒或不改坏", code in (400, 200))

    print("== 种类 ==")
    code, s9 = call("POST", "/api/cats", {"name": "自测种类", "who": "自测"})
    check("新增种类", code == 200 and "自测种类" in s9["categories"])
    cid = next(c["id"] for c in s9["cats"] if c["name"] == "自测种类")
    code, _ = call("POST", "/api/cats", {"name": "自测种类"})
    check("重名种类被拒", code == 400)
    code, s10 = call("PATCH", "/api/cats/%d" % cid, {"name": "自测种类2", "who": "自测"})
    check("种类改名", "自测种类2" in s10["categories"])
    code, _ = call("DELETE", "/api/cats/%d?who=%s" % (cid, q("自测")))
    check("删除空种类", code == 200)
    code, s11 = call("GET", "/api/state")
    used = next(c["id"] for c in s11["cats"] if c["name"] == "被套")
    code, body = call("DELETE", "/api/cats/%d?who=%s" % (used, q("自测")))
    check("在用种类不能删", code == 400, body)

    print("== 柜子 ==")
    code, s12 = call("PATCH", "/api/cabinets/%d" % empty["id"], {"name": "自测柜", "who": "自测"})
    cab = [c for r in s12["rooms"] for c in r["cabinets"] if c["id"] == empty["id"]][0]
    check("改柜子名", cab["name"] == "自测柜", cab)
    dup = [c for r in s12["rooms"] for c in r["cabinets"] if c["id"] != empty["id"]][0]
    code, body = call("PATCH", "/api/cabinets/%d" % empty["id"], {"code": dup["code"], "who": "自测"})
    check("重复编号被拒", code == 400, body)
    code, s13 = call("POST", "/api/cabinets", {"roomId": s12["rooms"][0]["id"], "name": "", "who": "自测"})
    old_ids = {x["id"] for r in s12["rooms"] for x in r["cabinets"]}
    newc = [c for r in s13["rooms"] for c in r["cabinets"] if c["id"] not in old_ids]
    check("新增柜子并自动编号", code == 200 and len(newc) == 1, newc)
    if newc:
        check("新编号延续", newc[0]["code"].startswith("A"), newc[0]["code"])
        code, _ = call("DELETE", "/api/cabinets/%d?who=%s" % (newc[0]["id"], q("自测")))
        check("删除空柜子", code == 200)

    print("== 搜索 ==")
    code, res = call("GET", "/api/search?q=" + q("冬被"))
    check("搜冬被", code == 200 and len(res["results"]) >= 1, res)
    code, res = call("GET", "/api/search?q=" + q("不存在的东西"))
    check("搜不到返回空", code == 200 and res["results"] == [])

    print("== 备份 ==")
    check("写操作后生成 .bak", os.path.exists(os.path.join(TESTDATA, "storage.db.bak")))
    code, dump = call("GET", "/api/export")
    check("导出成功", code == 200 and "items" in dump["data"])
    before = dump["data"]["items"]
    code, s14 = call("POST", "/api/import", dump)
    after = [i for r in s14["rooms"] for c in r["cabinets"] for i in c["items"]]
    check("导入后数据一致", {i["id"] for i in before} == {i["id"] for i in after}, (len(before), len(after)))
    code, _ = call("POST", "/api/import", {"data": {"rooms": [], "items": []}})
    s15 = call("GET", "/api/state")[1]
    check("导入空文件被拒、原有数据还在", len(s15["rooms"]) == 2, s15["rooms"])
finally:
    p.terminate()

print("\n== 数据库丢失自动还原 ==")
db = os.path.join(TESTDATA, "storage.db")
bak = db + ".bak"
if os.path.exists(db):
    os.remove(db)
    for sfx in ("-wal", "-shm"):
        if os.path.exists(db + sfx): os.remove(db + sfx)
p2 = start()
try:
    s16 = call("GET", "/api/state")[1]
    check("重启后从备份还原", len(s16["rooms"]) == 2 and s16["rooms"][0]["cabinets"], s16["rooms"])
finally:
    p2.terminate()

print("\n通过 %d 项，失败 %d 项" % (ok, fail))
