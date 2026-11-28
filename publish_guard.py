# -*- coding: utf-8 -*-
"""发布守卫：保证每次发布都不替换用户数据

用法（发布流程里按顺序跑）：
  python publish_guard.py pre     # 发布前：冻结线上数据基准 + 同步到本地 + 一致性自检
  python publish_guard.py post    # 发布后：校验线上数据没少；少了自动从基准恢复

基准文件：D:/WorkData/quiltmap-backup/publish-baseline.json
每次 pre 都会用「线上当前最新数据」重写基准，所以基准永远是发布那一刻的用户真实数据。
post 发现线上数据比基准少（被发布包旧数据覆盖），立刻 POST /api/import 把基准灌回去。
"""
import json, os, sqlite3, sys, time, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(os.path.dirname(BASE), "quiltmap-backup")
BASELINE = os.path.join(BACKUP, "publish-baseline.json")
HOST = "https://quiltmap.app.workbuddy.host"
CK = os.path.join(BACKUP, "_ck3.txt")
TABLES = ("rooms", "cabinets", "types", "cats", "items", "logs", "config")


def cookie():
    s = ""
    for line in open(CK, encoding="utf-8").read().splitlines():
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#") or not line:
            continue
        f = line.split("\t")
        if len(f) >= 7:
            s += (f[5] + "=" + f[6] + "; ")
    return s.strip("; ")


def export_live():
    req = urllib.request.Request(HOST + "/api/export", headers={"Cookie": cookie()})
    payload = json.load(urllib.request.urlopen(req, timeout=40))
    return payload.get("data") or payload


def counts(data):
    return {t: len(data.get(t, [])) for t in TABLES}


def local_counts():
    db = os.path.join(BASE, "data", "storage.db")
    c = sqlite3.connect(db)
    n = {t: c.execute("SELECT COUNT(*) FROM " + t).fetchone()[0] for t in TABLES}
    c.close()
    return n


def save_baseline(data):
    os.makedirs(BACKUP, exist_ok=True)
    payload = {"frozen_at": time.strftime("%Y-%m-%d %H:%M:%S"), "data": data}
    with open(BASELINE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    with open(os.path.join(BACKUP, "baseline-%s.json" % stamp), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return payload["frozen_at"]


def load_baseline():
    if not os.path.exists(BASELINE):
        raise SystemExit("没有基准文件，不能校验；先跑 pre")
    return json.load(open(BASELINE, encoding="utf-8"))["data"]


def missing_keys(base, live):
    """基准里有、线上没有的记录（按主键）。空＝没丢数据。
    logs 例外：系统从 v1161 起只保留最近 10 条动态，更早的属正常清理，不算丢失。"""
    PK = {"rooms": "id", "cabinets": "id", "types": "id", "cats": "name",
          "items": "id", "logs": "id", "config": "key"}
    LOG_KEEP = 10
    miss = {}
    for t in TABLES:
        pk = PK[t]
        rows = base.get(t, [])
        if t == "logs":
            rows = sorted(rows, key=lambda r: r.get("id") or 0)[-LOG_KEEP:]
        b = {str(r.get(pk)) for r in rows}
        l = {str(r.get(pk)) for r in live.get(t, [])}
        if b - l:
            miss[t] = sorted(b - l)
    return miss


def pre():
    live = export_live()
    frozen = save_baseline(live)
    print("基准已冻结于", frozen, counts(live))
    # 同步到本地
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(BASE, "sync_from_live.py")],
                       capture_output=True, text=True)
    print(r.stdout.strip())
    if r.returncode != 0:
        raise SystemExit("!! 同步失败，禁止发布：" + r.stderr.strip())
    lc = local_counts()
    if lc != counts(live):
        raise SystemExit("!! 本地与线上不一致，禁止发布：本地=%s 线上=%s" % (lc, counts(live)))
    print("✓ 发布前校验通过：本地数据与线上完全一致，可以发布")


def post():
    base = load_baseline()
    live = export_live()
    print("发布后线上：", counts(live), " 基准：", counts(base))
    miss = missing_keys(base, live)
    if not miss:
        print("✓ 发布后校验通过：用户数据一条没少")
        return
    print("!! 发现数据被覆盖，缺失：", {t: len(v) for t, v in miss.items()})
    body = json.dumps({"data": base}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(HOST + "/api/import", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Cookie": cookie()})
    json.load(urllib.request.urlopen(req, timeout=60))
    restored = export_live()
    still = missing_keys(base, restored)
    if still:
        raise SystemExit("!! 恢复后仍有缺失：%s 请人工检查" % still)
    print("✓ 已从基准自动恢复用户数据：", counts(restored))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "pre"
    {"pre": pre, "post": post}[cmd]()
