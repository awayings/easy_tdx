"""公共舆情模块测试（无网络：monkeypatch 抓取函数）。"""

from __future__ import annotations

import easy_tdx.public_sentiment as ps


def test_symbol_format():
    assert ps._symbol("SZ", "002594") == "SZ002594"
    assert ps._symbol("0", "002594") == "SZ002594"
    assert ps._symbol("SH", "600519") == "SH600519"
    assert ps._symbol("1", "600519") == "SH600519"


def test_analyze_with_hot_rank_hit(monkeypatch):
    monkeypatch.setattr(ps, "fetch_stock_news", lambda code: None)
    hot = [
        {"symbol": "SH600000", "rank": 1, "rank_change": 0},
        {"symbol": "SZ002594", "rank": 42, "rank_change": 15},
    ]
    r = ps.analyze_stock_public("SZ", "002594", hot_rank=hot)
    assert r["hot_rank"] == 42
    assert r["hot_rank_change"] == 15
    assert r["available"] is True
    assert r["news_hits_total"] is None


def test_analyze_not_in_hot_rank(monkeypatch):
    monkeypatch.setattr(
        ps, "fetch_stock_news", lambda code: {"hits_total": 10, "articles": [], "sentiment": 0.0}
    )
    hot = [{"symbol": "SH600000", "rank": 1, "rank_change": 0}]
    r = ps.analyze_stock_public("SZ", "002594", hot_rank=hot)
    assert r["hot_rank"] is None
    assert r["available"] is True  # 新闻源仍可用
    assert r["news_hits_total"] == 10


def test_analyze_all_unavailable(monkeypatch):
    monkeypatch.setattr(ps, "fetch_hot_rank", lambda: None)
    monkeypatch.setattr(ps, "fetch_stock_news", lambda code: None)
    r = ps.analyze_stock_public("SZ", "002594")
    assert r["available"] is False


def test_news_articles_3d_count(monkeypatch):
    news = {
        "hits_total": 5,
        "sentiment": 0.2,
        "articles": [
            {"date": "2026-09-02 09:01:00", "title": "a", "summary": ""},
            {"date": "2026-09-01 19:15:00", "title": "b", "summary": ""},
            {"date": "2026-08-20 10:00:00", "title": "c", "summary": ""},  # 超 3 日
            {"date": "bad", "title": "d", "summary": ""},  # 解析失败忽略
        ],
    }
    monkeypatch.setattr(ps, "fetch_stock_news", lambda code: news)
    r = ps.analyze_stock_public("SH", "600519", hot_rank=[])
    assert r["news_articles_3d"] == 2


def test_fetch_stock_news_sentiment_scoring(monkeypatch):
    """新闻情绪打分：标题关键词计分（模拟抓取结果的结构校验）。"""
    captured: dict = {}

    class _Resp:
        def __init__(self):
            self._data = (
                'cb({"hitsTotal": 3, "result": {"cmsArticleWebOld": ['
                '{"date": "2026-09-02 09:00:00", "title": "公司中标大单 业绩预增", '
                '"content": "", "code": "x", "url": ""},'
                '{"date": "2026-09-01 09:00:00", "title": "股东减持公告", '
                '"content": "", "code": "x", "url": ""},'
                '{"date": "2026-08-30 09:00:00", "title": "中性消息", '
                '"content": "", "code": "x", "url": ""}'
                "]}})"
            ).encode()

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(ps.urllib.request, "urlopen", fake_urlopen)
    news = ps.fetch_stock_news("600519", page_size=5)
    assert news is not None
    assert news["hits_total"] == 3
    assert news["pos"] == 1 and news["neg"] == 1
    assert news["sentiment"] == 0.0  # (1-1)/2
    assert "600519" in captured["url"]
