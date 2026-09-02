"""个股公共舆情（docs/trading_system_tasks.md P2-T5 公开可抓源替代）。

无 tushare、无付费源、无登录态——全部东方财富公开免费接口（2026-09-02 实测连通）：

- **人气榜**：POST ``https://emappdata.eastmoney.com/stockrank/getAllCurrentList``
  （全局热度前 N 名 + 排名变化）。个股在人气榜的排名/排名变化 = 公众关注度
  代理：排名越靠前、排名上升越快，情绪越亢奋（反身性风险越高）。
- **个股新闻**：GET ``https://search-api-web.eastmoney.com/search/jsonp``
  （keyword=代码，sort=time），返回命中数与近期新闻；标题/摘要按利好/利空
  词表打分得到新闻情绪 -1~1（粗粒度、仅供风险提示，不做信号）。

所有 fetch 失败返回 None/空结构，绝不抛异常——离线时交易计划降级为
"仅技术面+市场情绪"，舆情风险计 0 分。TDX 防封红线与本模块无关（东财接口），
但每只股票一次计划请求不超过 2 次 HTTP。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

HOT_RANK_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
NEWS_SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"

# 利好/利空词表（标题+摘要关键词，粗粒度方向判断）
POSITIVE_KEYWORDS = (
    "预增", "增长", "中标", "回购", "增持", "获批", "签订", "突破", "新高",
    "涨价", "盈利", "扭亏", "分红", "重组", "订单", "签约", "量产", "投产",
    "合作", "涨停", "净买入", "上调",
)
NEGATIVE_KEYWORDS = (
    "下降", "下滑", "亏损", "减持", "立案", "处罚", "诉讼", "终止", "下调",
    "退市", "违约", "质押", "商誉", "爆雷", "违规", "警示", "问询", "预亏",
    "停产", "召回", "事故", "跌停", "风险", "净卖出",
)

_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.eastmoney.com/"}


def _get_json(url: str, timeout: float = 10.0) -> dict | None:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> dict | None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={**_HEADERS, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def fetch_hot_rank(page_size: int = 100) -> list[dict] | None:
    """东财人气榜前 page_size 名。

    Returns:
        [{symbol: "SH600127", rank, rank_change}] 或 None（失败）。
    """
    try:
        data = _post_json(
            HOT_RANK_URL,
            {
                "appId": "appId01",
                "globalId": "786e4c21-70dc-435a-93bb-38",
                "marketType": "",
                "pageNo": 1,
                "pageSize": page_size,
            },
        )
        rows = (data or {}).get("data") or []
        return [
            {"symbol": r["sc"], "rank": int(r["rk"]), "rank_change": int(r.get("rc") or 0)}
            for r in rows
        ]
    except Exception:
        return None


def fetch_stock_news(code: str, page_size: int = 10) -> dict | None:
    """个股新闻搜索（东财公开搜索接口，按时间倒序）。

    Returns:
        {hits_total, articles: [{date, title, summary}], sentiment, pos, neg}
        或 None（失败）。
    """
    param = json.dumps(
        {
            "uid": "",
            "keyword": code,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "time",
                    "pageIndex": 1,
                    "pageSize": page_size,
                    "preTag": "<em>",
                    "postTag": "</em>",
                }
            },
        },
        ensure_ascii=False,
    )
    url = f"{NEWS_SEARCH_URL}?cb=cb&param={urllib.parse.quote(param)}"
    try:
        text = ""
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            text = resp.read().decode("utf-8", "ignore")
        body = json.loads(text[text.index("(") + 1 : text.rindex(")")])
        articles = []
        for a in (body.get("result") or {}).get("cmsArticleWebOld") or []:
            title = re.sub(r"</?em>", "", a.get("title") or "")
            summary = re.sub(r"</?em>", "", a.get("content") or "")
            articles.append({"date": a.get("date") or "", "title": title, "summary": summary})
        pos = sum(
            1
            for a in articles
            if any(k in a["title"] + a["summary"] for k in POSITIVE_KEYWORDS)
        )
        neg = sum(
            1
            for a in articles
            if any(k in a["title"] + a["summary"] for k in NEGATIVE_KEYWORDS)
        )
        denom = max(pos + neg, 1)
        return {
            "hits_total": int(body.get("hitsTotal") or 0),
            "articles": articles,
            "sentiment": round((pos - neg) / denom, 2),
            "pos": pos,
            "neg": neg,
        }
    except Exception:
        return None


def _symbol(market: str, code: str) -> str:
    """市场+代码 → 东财证券代码格式（SH600519 / SZ002594）。"""
    m = market.upper()
    if m in ("0", "SZ"):
        return f"SZ{code}"
    if m in ("1", "SH"):
        return f"SH{code}"
    return f"{m}{code}"


def analyze_stock_public(market: str, code: str, hot_rank: list[dict] | None = None) -> dict:
    """个股公共舆情画像：人气榜排名/变化 + 近期新闻数量与情绪。

    Args:
        market: 市场（SZ/SH/0/1）。
        code: 6 位代码。
        hot_rank: 已抓取的人气榜（复用避免重复请求）；None 则本函数抓取。

    Returns:
        {hot_rank, hot_rank_change, news_hits_total, news_articles_3d,
         news_sentiment, available}；失败项为 None，available=False 表示
        两项舆情源均不可用（风险评分时计 0 分）。
    """
    result: dict = {
        "hot_rank": None,
        "hot_rank_change": None,
        "news_hits_total": None,
        "news_articles_3d": 0,
        "news_sentiment": None,
        "available": False,
    }
    sym = _symbol(market, code)

    if hot_rank is None:
        hot_rank = fetch_hot_rank()
    if hot_rank is not None:
        for r in hot_rank:
            if r["symbol"] == sym:
                result["hot_rank"] = r["rank"]
                result["hot_rank_change"] = r["rank_change"]
                result["available"] = True
                break

    news = fetch_stock_news(code)
    if news is not None:
        result["news_hits_total"] = news["hits_total"]
        result["news_sentiment"] = news["sentiment"]
        result["available"] = True
        # 近 3 日新闻条数（date 形如 "2026-09-02 09:01:00"）
        from datetime import datetime, timedelta

        cutoff = datetime.now() - timedelta(days=3)
        for a in news["articles"]:
            try:
                d = datetime.strptime((a.get("date") or "")[:10], "%Y-%m-%d")
            except ValueError:
                continue
            if d >= cutoff:
                result["news_articles_3d"] += 1
    return result
