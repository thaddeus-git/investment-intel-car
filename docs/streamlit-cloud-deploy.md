# Streamlit Community Cloud 部署须知

本地能跑、上云报错，且报错每次不一样、难以预判——根因是本地心智模型与 Cloud 运行模型
的系统性偏差。本页列出要点，避免反复踩坑。

## 核心认知：Cloud 是「从 git 重建的临时容器」

- **从 git 克隆部署**：仓库里没有的东西，云上就没有。`data/competitor_intel.db`
  被 `.gitignore` 排除，导致云上没有 DB → `sqlite3.OperationalError`。
- **运行时文件系统是临时的**：redeploy / Reboot / 休眠唤醒都会重建容器，运行时写入的
  文件（包括运行时新建的 DB）全部丢失。所以云上 dashboard 是**只读展示**，DB 必须从
  git 来，不能靠云上跑采集生成。
- **无持久磁盘**：Community Cloud 没有挂载持久卷，要持久化状态得用外部托管 DB。
- **CWD 永远是仓库根**（不是 `src/`）。`config.py:64` 用 `Path(__file__)` 派生绝对路径
  已解决这点。
- **Python 版本部署时钉死**：当前云上是 3.14。改版本必须删 app 重新部署，不能事后改。

## 数据更新流程

云上数据靠 `scripts/refresh_cloud_db.sh` 更新：

```
本地跑采集 → checkpoint WAL → git add -f data/competitor_intel.db → push
```

push 后去 Streamlit Cloud → Manage app → Reboot。

## Secrets

- 本地用 `.streamlit/secrets.toml`（已 gitignore）。
- **云上不读文件**，只能去 App settings → Secrets 框里粘贴 TOML。两处通过 `st.secrets`
  统一访问。

## 报错排查

- Cloud UI 会把错误 **redact**（"redacted to prevent data leaks"），看不到真因。
- 真实 traceback 在 **Manage app → Cloud logs**，可 Download log 下载到本地。
- 本仓库 `dashboard.py` 的 `check_db_ready()` 会在 DB 缺失/缺表时直接给中文 `st.error`
  并 `st.stop()`，避免触发 redacted 崩溃页。

## 依赖

- `requirements.txt` 在仓库根（6 个包）。注意 `streamlit` 云上预装，版本要钉。
- 需要系统库时用仓库根的 `packages.txt`（Debian bullseye，一行一个包名）。
