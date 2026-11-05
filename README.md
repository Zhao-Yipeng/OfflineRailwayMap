# OfflineRailwayMap

把 OpenRailwayMap 风格的铁路线路和最高速度（限速）缓存到本地、断网也能浏览的 Windows 桌面程序。

> OfflineRailwayMap 是独立的第三方程序，与 OpenRailwayMap 项目和 OpenStreetMap 基金会无关。

完整的需求与设计见 [SPEC.md](SPEC.md)。

## 功能

- 地图按 Web 墨卡托 z8 瓦片划分成方格（赤道处约 156.5 km，纬度 40° 处约 120 km），覆盖全球。
- **编辑模式**：单击选择方格，Shift + 拖动框选，Alt + 拖动框选取消；右键已缓存的方格可以更新或删除。
- **开始浏览**：选中的方格在后台下载（离线底图来自 Protomaps），下完一格就立即改用本地数据显示。铁路数据的来源自动选择：一次下载 6 格及以上时用 Geofabrik 的地区数据包（例如整个省一次下载，在本地筛出铁路），少于 6 格时用 Overpass API；数据包用不了时自动改用 Overpass。两者是同一份 OpenStreetMap 数据，显示效果相同，数据包每天更新一次。
- **缓存区域**：任何缩放级别都显示干线、支线、在建和规划线路，并按速度着色（与网页版相同的颜色）；z9 起标速度数字；站线、渡线等配线放大后才显示；离线底图和车站名断网也能用。
- **未缓存区域**：铁路与 OpenRailwayMap 网页版的「最高速度」图层一致，联网边看边加载；底图来自 OpenFreeMap，配色与离线底图相同。
- **永远显示在建线路**（设置中的开关，默认关，只对中国有效）：网页版从 z9 起才显示在建线路，而且只显示干线和支线。打开后，中国未缓存的区域也在所有缩放级别显示在建铁路，样式与已建成线路相同（按速度着色）。打开时程序下载中国各省的地区数据包（已有的直接用，和下载方格共用），提取出在建线路后询问是否立即删除专门为此下载的数据包。
- 搜索车站和地名、点击线路看详情、速度图例、设置。
- **清空缓存**：左下角按钮一键删除程序产生的全部数据，程序文件夹恢复到刚解压时的样子。

## 使用

1. 系统要求：Windows 10 / 11（64 位），显卡支持 WebGL 2。
2. 下载 `OfflineRailwayMap-<版本>-win64.zip`，解压到一个**可写**的文件夹（例如 `D:\Tools\OfflineRailwayMap`，不要放在 `C:\Program Files` 下）。
3. 运行 `OfflineRailwayMap.exe`。程序未做代码签名，第一次运行时 Windows 可能弹出 SmartScreen 提示，选择「仍要运行」。

第一次打开处于编辑模式：放大到能看清网格后选择方格，点右下角「开始浏览」。下载进度显示在底部的下载面板中，可以暂停和继续；程序重启后会接着下载。

参考：一个方格的铁路数据通常为几 MB（北京市区所在的 210-97 格实测约 3 MB），离线底图（默认 z14）在北京一带约 25 MB、东京或德国鲁尔区约 120 MB。缓存整个中国（约 700 格）预计占用 3–8 GB。

## 数据保存在哪里（「不留痕」）

- 程序写入的一切都在程序文件夹下的 `data\` 中：设置、方格数据、离线底图、在线瓦片缓存、日志。程序不写注册表，也不写 `%APPDATA%`、`%LOCALAPPDATA%`、`%TEMP%` 等其他位置。
- `data\` 只在需要保存东西时才创建（例如在线浏览产生瓦片缓存、开始下载、修改设置）；只打开程序、不做任何操作就关闭，不会留下文件。
- 「清空缓存」会删除整个 `data\`，释放程序占用的全部硬盘空间。清空后直接退出，程序文件夹与刚解压时完全一致。清空后继续浏览，在线瓦片只放在内存里，不会重新写盘。
- **卸载**：关闭程序，删除整个程序文件夹即可（`data\` 在其中，不需要先清空缓存）。
- 以下由 Windows 或其他软件为任何运行过的程序自己产生的记录不受程序控制，通常只有几 KB：预读文件（Prefetch）、资源管理器的程序启动记录、防火墙和 SmartScreen 记录、系统错误报告（只在程序崩溃时）、杀毒软件日志。

## 数据来源与使用条款

| 数据 / 服务 | 用途 | 条款 |
|-------------|------|------|
| OpenRailwayMap 瓦片与搜索 API | 未缓存区域的速度图层、车站搜索 | 只限免费、公开的非商业程序使用；必须带本程序的 User-Agent；只按需请求；收到 429 停止访问。<https://wiki.openstreetmap.org/wiki/OpenRailwayMap/API> |
| OpenFreeMap 矢量瓦片 | 未缓存区域的灰色底图（按离线底图的配色显示） | 免费、不限访问量、不需要注册；必须署名。只按视野请求，按 HTTP 缓存头缓存（最多 7 天）。<https://openfreemap.org/tos/> |
| Overpass API | 下载方格的铁路数据（少于 6 格时） | 按每台服务器允许的名额并发，记录每天的用量。 |
| Geofabrik 地区数据包 | 下载方格的铁路数据（6 格及以上时）；「永远显示在建线路」的中国各省数据 | 免费下载，不要短时间内反复下载同一文件。同一地区只下载一次，筛出的铁路保留 7 天。<https://download.geofabrik.de/technical.html> |
| Protomaps 每日构建 | 离线底图 | 按方格一次性读取需要的部分。<https://docs.protomaps.com/basemaps/downloads> |
| Nominatim | 地名搜索 | 每秒最多一次，只在按回车时搜索。<https://operations.osmfoundation.org/policies/nominatim/> |

不使用 OpenStreetMap 官方瓦片服务器（tile.openstreetmap.org）：它的使用政策规定离线类程序不得使用，实测本程序的请求会被拦截。

地图数据 © OpenStreetMap contributors（ODbL）。在线底图 OpenFreeMap © OpenMapTiles。缓存区域的速度颜色、线宽和显示规则取自 [OpenRailwayMap-CartoCSS](https://github.com/OpenRailwayMap/OpenRailwayMap-CartoCSS)（GPL-3.0-or-later）。

所有请求都带 `OfflineRailwayMap/<版本> (+https://github.com/Zhao-Yipeng/task1)` 这个 User-Agent，不伪装成浏览器，也不伪造 Referer。

## 已知限制

- 铁路数据来自三台免费的公共 Overpass 服务器（OSM wiki 上全部免费、不需要密钥的全球实例），它们有时很忙。程序最多同时下载 3 格，按每台服务器自己允许的名额并发查询（overpass-api.de 要求不得并行，只用 1 个）；忙时自动等待、换服务器、拆分查询，实在不行方格会回到队列稍后重试，而不是判为失败。下载面板逐格显示正在等哪台服务器、等了多久、已接收多少数据。设置里可以调整服务器列表，例如加入需要注册密钥的服务器。
- 一次下载 6 格及以上时，程序先下载这些方格所在地区的数据包（省或国家，几十 MB 到 1 GB 多），筛出铁路后删除原文件，筛选结果在 `data\extracts\` 保留 7 天。下载中断会自动续传。如果一个数据包比它服务的方格大太多（平均每格超过 250 MB，例如只为一个边境方格下载整个邻国），这些方格仍用 Overpass。
- 如果下载长时间没有进展，请把程序文件夹里的 `data\logs\app.log` 发给开发者。
- 离线底图只有一个最大级别：如果不同方格是用不同的「离线底图最大级别」下载的，按其中最低的显示。
- 离线底图里的阿拉伯文、希伯来文等从右到左的文字没有做字形连接处理。

## 开发

需要 Python 3.12 或更新版本。

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -e .[dev]

python -m offline_railway_map           # 打开程序窗口
python -m offline_railway_map --serve   # 只启动本地服务，在浏览器中打开输出的地址
python -m pytest                        # 单元测试和集成测试
```

- 端到端测试（需要 Node.js 和 Playwright）：`PYTHON=<python 路径> node tests/e2e/smoke.cjs`，以及同目录下的 `cells.cjs`、`features.cjs`、`details.cjs`、`construction.cjs`、`online.cjs`、`download.cjs`（后两个会访问真实服务器；`download.cjs` 可以用环境变量 `E2E_SETTINGS` 传入 settings.ini 内容，例如把 Overpass 指向本地替身服务器）。
- Qt 窗口冒烟测试：`QT_QPA_PLATFORM=offscreen python tests/qt_smoke.py`。
- 第三方前端资源：`python tools/vendor_assets.py`（固定版本并校验哈希）；离线底图样式由 `tools/build_basemap_style.mjs` 生成。
- 打包（Windows）：`pyinstaller packaging/OfflineRailwayMap.spec --noconfirm`，输出在 `dist\OfflineRailwayMap\`。必须使用单文件夹模式。
- GitHub Actions（`.github/workflows/build.yml`）在 Windows 上运行测试、打包，并上传 zip。

## 许可证

GPL-3.0，见 [LICENSE](LICENSE)。第三方组件的许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
