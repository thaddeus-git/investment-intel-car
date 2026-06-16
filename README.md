# 竞品情报监控系统

自动化监控 5 家美股二手车赛道竞品（CVNA/KMX/AN/UXIN/ATHM）的 SEC 动向，
生成中文 Streamlit 看板。

## 功能

- Phase 1: SEC Filing 采集 / XBRL 财务提取 / 8-K 事件预警 / Earnings Call 纪要
- Phase 2: Form 4 内部人交易 / Form 144 减持计划 / 13F 机构持仓 / 情绪指标

## 运行

```bash
pip install edgartools streamlit plotly pandas openai schedule
cd src
streamlit run dashboard.py
```
