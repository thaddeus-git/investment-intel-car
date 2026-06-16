"""
竞品情报监控系统 — 定时调度器

- collector:              每 6 小时（SEC filing 轮询）
- summarizer:             每 30 分钟（LLM 摘要比 SEC 轮询更频繁）
- insider_tracker:        每日一次（Form 4/144 内部人交易，不需要高频）
- institutional_tracker:  每季度一次（13F 季报，季末+50天触发）
                          或通过手工触发（python -m institutional_tracker）
"""

import logging
import time
from datetime import datetime

import schedule

from collector import collect_all
from summarizer import summarize_new
from config import SCHEDULE_13F

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def _run_insider():
    """Wrapper: 内部人交易采集。"""
    try:
        from insider_tracker import collect_all_insider
        logger.info("Running insider tracker...")
        collect_all_insider()
    except Exception as e:
        logger.error("Insider tracker failed: %s", e)


def _run_institutional():
    """Wrapper: 13F 机构持仓采集（仅季度触发时调用）。"""
    now = datetime.now()
    # 检查是否在 13F 采集窗口内（季末+50天 往后 7 天内）
    for q, (end_m, end_d, trigger_m, trigger_d) in SCHEDULE_13F.items():
        trigger_date = datetime(now.year, trigger_m, trigger_d)
        # 如果 trigger 在明年（Q4），年份需要调整
        if trigger_m < end_m:
            trigger_date = datetime(now.year + 1, trigger_m, trigger_d)

        days_since_trigger = (now - trigger_date).days
        if 0 <= days_since_trigger < 7:
            try:
                from institutional_tracker import collect_all_institutional
                logger.info("Running 13F institutional tracker (Q%d trigger)...", q)
                collect_all_institutional()
            except Exception as e:
                logger.error("Institutional tracker failed: %s", e)
            return

    logger.debug("Not in 13F collection window. Skipping institutional tracker.")


def main():
    schedule.every(6).hours.do(collect_all)
    schedule.every(30).minutes.do(summarize_new)
    schedule.every().day.at("09:00").do(_run_insider)       # 每日 9am
    schedule.every().day.at("18:00").do(_run_institutional)  # 每日检查是否进入 13F 窗口

    # 启动时先跑一次，确保看板立刻有数据
    logger.info("Initial run: collector")
    collect_all()
    logger.info("Initial run: summarizer")
    summarize_new()
    logger.info("Initial run: insider tracker")
    _run_insider()

    logger.info(
        "Scheduler started. collector/6h, summarizer/30min, "
        "insider/daily@9am, 13F/daily-check@6pm"
    )
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
