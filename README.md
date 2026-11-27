# 家庭储物管家 · 家里的柜子

把家里的柜子做成可视化清单：哪个柜子放了什么、几件、谁的，一眼就能看到。适合多人（一家人）共用同一份数据，随时增删改。

## 功能

- **柜子可视化**：两个房间各 5 个柜子，编号 A1~A5 / B1~B5；柜门做成半透明玻璃质感，透过门就能看到柜子里的物品名称和数量，鼠标移上去柜门会微微打开
- **物品库（基础数据）**：物品名称、分类统一在「物品库」里维护，改名/改分类后所有柜子里的同名物品同步更新；柜子里放东西时直接从物品库选择，同一种物品重复放入自动累加数量
- **柜子可编辑**：柜子可改名称、改编号、增删；有物品的柜子要先清空才能删
- **房间可改名**：主卧 / 次卧 名称可改
- **搜索**：输入「冬被」直接定位到房间 + 柜号，点结果跳到对应柜子
- **多人协作**：页面每 20 秒静默同步；每次增删改都会记「最近动态」，标注是谁操作的

## 技术栈

- 后端：Python 3 标准库（`http.server` + `sqlite3`），零第三方依赖
- 前端：原生 HTML/CSS/JS 单文件，移动端自适应
- 数据：SQLite（`data/storage.db`），首次启动自动建库并写入 2 房间 × 5 柜与常见床品种子数据

## 运行

```bash
python3 server.py            # 默认 3000 端口
PORT=8080 python3 server.py  # 指定端口
```

打开 http://127.0.0.1:3000 即可使用。服务监听 `0.0.0.0`，可直接部署到任意支持单端口 HTTP 服务的平台。

## 数据模型

| 表 | 说明 |
| --- | --- |
| `rooms` | 房间（主卧 / 次卧） |
| `cabinets` | 柜子，`code` 为家中唯一编号，`name` 是柜子名称 |
| `types` | 物品基础库：名称 + 分类（被子/床单/被套/枕头/毛毯/其他） |
| `items` | 柜内条目：引用 `types`，记录数量、备注、操作人 |
| `logs` | 操作动态（谁、做了什么、时间） |

旧版数据库（物品自带名称）在启动时会自动迁移到「基础库 + 引用」结构，不会丢数据。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 全部数据（房间/柜子/柜内物品/物品库/动态） |
| GET | `/api/search?q=` | 按物品名、分类、备注、柜号搜索 |
| POST | `/api/types` | 新增物品库条目 `{name, category}` |
| PATCH | `/api/types/{id}` | 改物品名称/分类（同步柜内） |
| DELETE | `/api/types/{id}` | 删物品库条目（在使用中会拒绝） |
| POST | `/api/items` | 往柜子里放物品 `{cabinetId, typeId, qty, note, who}` |
| PATCH | `/api/items/{id}` | 改数量 / 备注 |
| DELETE | `/api/items/{id}?who=` | 从柜子取出 |
| POST | `/api/cabinets` | 新增柜子 `{roomId, name}`（编号自动顺延） |
| PATCH | `/api/cabinets/{id}` | 改柜子名称 / 编号 |
| DELETE | `/api/cabinets/{id}` | 删柜子（需先清空） |
| PATCH | `/api/rooms/{id}` | 房间改名 |

## 目录

```
server.py          # 后端服务（含建库、迁移、种子数据、路由）
static/index.html  # 前端单页（样式 + 交互全部内联）
data/storage.db    # 运行后生成的 SQLite 数据库（不入库）
```

## 说明

- 物品与柜子是「基础数据」，建议先在物品库把家里常见的床品建全，之后放柜子时点选即可
- 数据存服务端单文件 SQLite，多人访问同一份；备份直接复制 `data/storage.db`

## 发布流程（务必遵守：绝不覆盖用户数据）

发布包里带的是本地 `data/storage.db`，直接上传会把线上用户这段时间的增删改冲掉。
所以每次发布前后必须用守卫脚本，它会把「线上当前真实数据」冻结成基准，同步到本地，
发布后再核对；万一数据被旧包覆盖，自动从基准恢复。

```bash
# 1. 发布前：冻结线上数据基准 + 同步到本地 + 自检
python publish_guard.py pre        # 看到「✓ 发布前校验通过」才继续

# 2. 发布（更新线上程序，链接不变）
#    workbuddy_sites_deploy(directory=本目录, startCmd="python server.py")

# 3. 发布后：核对用户数据，少了就自动恢复
python publish_guard.py post       # 看到「✓ 发布后校验通过」才算完成
```

- 基准文件：`D:/WorkData/quiltmap-backup/publish-baseline.json`（每次 pre 自动重写为最新）
- 历史基准：同目录 `baseline-时间戳.json`，需要时可用 `sync_from_live.py --from 文件` 恢复
- 手动恢复线上：`publish_guard.py` 的 post 会自动做；只需回滚时把某份基准复制成 publish-baseline.json 再跑 post
