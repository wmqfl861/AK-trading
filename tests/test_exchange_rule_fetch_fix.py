import base64
import json
import subprocess
from datetime import datetime, timezone

import pytest

import ak_trading.exchange_rule_sources as source_module
from ak_trading.exchange_rule_sources import (
    DocumentFetchError,
    ExchangeSource,
    LocalWebSearchDocumentFetcher,
    ProductSource,
    collect_evidence,
)
from ak_trading.exchange_rules import RuleStatus


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
SOURCE = ExchangeSource(
    code="SHFE",
    official_domains=("shfe.com.cn",),
    products=(
        ProductSource(
            code="RB",
            contract="SHFE.rb2610",
            query="site:shfe.com.cn rb rules",
        ),
    ),
)
URL = "https://www.shfe.com.cn/rules.html"


def _completed(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["websearch"],
        returncode=0,
        stdout=json.dumps(payload, ensure_ascii=False),
        stderr="",
    )


def _fetch(payload: object) -> object:
    return LocalWebSearchDocumentFetcher(
        executable="websearch",
        runner=lambda command, timeout: _completed(payload),
        now=lambda: NOW,
    ).fetch(URL)


def test_empty_markdown_uses_raw_html_fallback_and_retains_bytes():
    html = "<html><body><h1>交易规则</h1><p>Fee schedule.</p></body></html>".encode(
        "gb2312"
    )
    document = _fetch(
        {
            "success": True,
            "data": {
                "markdown": "   ",
                "rawHtml": list(html),
                "metadata": {
                    "sourceURL": URL,
                    "title": "交易规则",
                    "charset": "gb2312",
                },
            },
        }
    )

    payload = json.loads(document.content)
    assert payload["markdown"] == "交易规则\nFee schedule."
    assert payload["fallback"]["field"] == "rawHtml"
    assert payload["fallback"]["charset"] == "gb2312"
    assert payload["fallback"]["charset_source"] == "metadata"
    assert "raw_html" in payload["fallback"]
    assert base64.b64decode(payload["fallback"]["raw_bytes_base64"]) == html


def test_fallback_html_with_malformed_marked_section_is_retained():
    html = "<![S bogus marked section]><p>Fee schedule.</p>"

    document = _fetch(
        {
            "success": True,
            "data": {
                "markdown": "",
                "html": html,
                "metadata": {"sourceURL": URL, "title": "Rules"},
            },
        }
    )

    payload = json.loads(document.content)
    assert "Fee schedule." in payload["markdown"]
    assert payload["fallback"]["field"] == "html"


def _forbid_stack(command, timeout):
    raise RuntimeError("stack must not be used")


class _FakeResponse:
    def __init__(self, payload: bytes, final_url: str, charset: str | None = None):
        self._payload = payload
        self.url = final_url
        headers = self
        self.headers = headers
        self._charset = charset

    def geturl(self) -> str:
        return self.url

    def get_content_charset(self) -> str | None:
        return self._charset

    def read(self, limit: int = -1) -> bytes:
        return self._payload if limit < 0 else self._payload[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _direct_fetch(monkeypatch, response, url, official_domains=("shfe.com.cn",)):
    import ak_trading.exchange_rule_sources as module

    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda request, timeout: response,
    )
    fetcher = LocalWebSearchDocumentFetcher(
        executable="websearch",
        runner=_forbid_stack,
        now=lambda: NOW,
        official_domains=official_domains,
    )
    return fetcher.fetch(url)


def test_direct_csv_fetch_passes_through_and_records_final_url(
    monkeypatch: pytest.MonkeyPatch,
):
    csv_bytes = "期货合约,保证金\nrb2610,10%\n".encode("gbk")
    response = _FakeResponse(
        csv_bytes,
        "https://www.shfe.com.cn/final/20260826_1.csv",
        charset="gbk",
    )

    document = _direct_fetch(
        monkeypatch,
        response,
        "http://www.shfe.com.cn/sj/jscs/20260826_1.csv",
    )

    payload = json.loads(document.content)
    assert payload["retrieval_method"] == "direct_structured_file"
    assert payload["source_url"].startswith("https://www.shfe.com.cn/final/")
    assert payload["requested_url"].startswith("http://www.shfe.com.cn/sj/")
    assert "rb2610" in payload["markdown"]
    assert document.url.startswith("https://www.shfe.com.cn/final/")


def test_direct_fetch_rejects_cross_domain_redirect(
    monkeypatch: pytest.MonkeyPatch,
):
    response = _FakeResponse(
        b"a,b\n1,2\n",
        "https://attacker.example/payload.csv",
    )

    # The rejection must fall through to the ordinary stack path; the
    # fail-runner proves no document was retained from the redirect.
    with pytest.raises(RuntimeError, match="stack must not be used"):
        _direct_fetch(
            monkeypatch,
            response,
            "http://www.shfe.com.cn/sj/jscs/20260826_1.csv",
        )


def test_direct_fetch_rejects_html_error_page(monkeypatch: pytest.MonkeyPatch):
    response = _FakeResponse(
        "<html><title>error</title></html>".encode("gbk"),
        "http://www.shfe.com.cn/sj/jscs/20260826_1.csv",
        charset="gbk",
    )

    with pytest.raises(RuntimeError, match="stack must not be used"):
        _direct_fetch(
            monkeypatch,
            response,
            "http://www.shfe.com.cn/sj/jscs/20260826_1.csv",
        )


def test_fallback_html_whose_extracted_text_is_challenge_is_skipped():
    challenge_html = (
        "<html><script>$_ts=window['$_ts'];if(!$_ts)$_ts={};"
        "$_ts.cd='obfuscated';</script></html>"
    )
    with pytest.raises(DocumentFetchError, match="empty normalized content"):
        _fetch(
            {
                "success": True,
                "data": {
                    "markdown": "",
                    "html": challenge_html,
                    "metadata": {"sourceURL": URL, "title": "Rules"},
                },
            }
        )


def test_metadata_declared_latin_1_is_not_marked_as_last_resort():
    html = b'<html><body><p>R\xe9sum\xe9</p></body></html>'
    document = _fetch(
        {
            "success": True,
            "data": {
                "markdown": "",
                "rawHtml": list(html),
                "metadata": {
                    "sourceURL": URL,
                    "title": "Rules",
                    "charset": "iso-8859-1",
                },
            },
        }
    )

    payload = json.loads(document.content)
    assert payload["markdown"] == "R\u00e9sum\u00e9"
    assert payload["fallback"]["charset"] == "iso-8859-1"
    assert payload["fallback"]["charset_source"] == "metadata"


def test_html_meta_charset_is_recorded_as_provenance():
    html = (
        b'<html><head><meta charset="windows-1252"></head>'
        b'<body><p>R\xe9sum\xe9</p></body></html>'
    )
    document = _fetch(
        {
            "success": True,
            "data": {
                "markdown": "",
                "rawHtml": list(html),
                "metadata": {"sourceURL": URL, "title": "Rules"},
            },
        }
    )

    payload = json.loads(document.content)
    assert payload["markdown"] == "R\u00e9sum\u00e9"
    assert payload["fallback"]["charset"] == "windows-1252"
    assert payload["fallback"]["charset_source"] == "html-meta"


def test_latin_1_last_resort_is_marked_as_fallback():
    document = _fetch(
        {
            "success": True,
            "data": {
                "markdown": "",
                "text": [0xFF],
                "metadata": {"sourceURL": URL, "title": "Rules"},
            },
        }
    )

    payload = json.loads(document.content)
    assert payload["markdown"] == "\u00ff"
    assert payload["fallback"]["charset"] == "latin-1"
    assert payload["fallback"]["charset_source"] == "fallback"


def test_empty_markdown_uses_nonempty_alternate_normalized_text():
    document = _fetch(
        {
            "success": True,
            "data": {
                "markdown": "",
                "text": "Short official notice",
                "metadata": {"sourceURL": URL, "title": "Notice"},
            },
        }
    )

    payload = json.loads(document.content)
    assert payload["markdown"] == "Short official notice"
    assert payload["fallback"]["field"] == "text"


def test_empty_cli_response_requests_rich_local_fallback(monkeypatch):
    calls = []
    responses = iter(
        [
            _completed(
                {
                    "success": True,
                    "data": {
                        "markdown": "",
                        "metadata": {"sourceURL": URL, "title": "Notice"},
                    },
                }
            ),
            _completed(
                {
                    "metadata": {"sourceURL": URL, "title": "Notice"},
                    "html": "<p>Rich local evidence</p>",
                }
            ),
        ]
    )
    monkeypatch.setattr(
        source_module.shutil,
        "which",
        lambda name: "firecrawl.cmd" if name == "firecrawl" else name,
    )

    def runner(command, timeout):
        calls.append(command)
        return next(responses)

    # Proxy-rendered content no longer short-circuits the humanlike tier, so
    # this test pins the rich-fallback mechanics with that tier disabled.
    document = LocalWebSearchDocumentFetcher(
        executable="websearch",
        runner=runner,
        now=lambda: NOW,
        humanlike_enabled=False,
    ).fetch(URL)

    payload = json.loads(document.content)
    assert payload["markdown"] == "Rich local evidence"
    assert payload["fallback"]["field"] == "html"
    assert payload["rich_retry_attempted"] is True
    assert payload["rich_retry_error"] is None
    assert calls[0] == ["websearch", "scrape", URL, "--stealth"]
    assert calls[1][:6] == [
        "firecrawl.cmd",
        "--api-url",
        "http://127.0.0.1:3002",
        "-k",
        "fc-local",
        "scrape",
    ]


def test_rich_retry_reports_missing_executable(monkeypatch):
    monkeypatch.setattr(source_module.shutil, "which", lambda name: None)

    result = source_module._rich_scrape_data(
        URL,
        timeout=10,
        runner=lambda command, timeout: pytest.fail("runner must not be called"),
    )

    assert result.data is None
    assert result.attempted is False
    assert result.error == "executable-not-found"


def test_rich_retry_reports_transport_failure(monkeypatch):
    monkeypatch.setattr(
        source_module.shutil,
        "which",
        lambda name: "firecrawl.cmd" if name == "firecrawl" else None,
    )

    def fail_transport(command, timeout):
        raise OSError("cannot start")

    result = source_module._rich_scrape_data(
        URL,
        timeout=10,
        runner=fail_transport,
    )

    assert result.data is None
    assert result.attempted is True
    assert result.error == "transport-error: OSError"


def test_empty_normalized_and_fallback_content_still_raises():
    with pytest.raises(DocumentFetchError, match="empty normalized content"):
        _fetch(
            {
                "success": True,
                "data": {
                    "markdown": "\n\t",
                    "html": " ",
                    "rawHtml": "",
                    "text": "\n",
                    "metadata": {"sourceURL": URL, "title": "Empty"},
                },
            }
        )


def test_normal_markdown_path_is_unchanged():
    document = _fetch(
        {
            "success": True,
            "data": {
                "markdown": "# Official rules\nFee: review this document.",
                "html": "<p>ignored alternate</p>",
                "metadata": {"sourceURL": URL, "title": "Official rules"},
                "fetcher": "scrapling",
            },
        }
    )

    payload = json.loads(document.content)
    assert payload["markdown"] == "# Official rules\nFee: review this document."
    assert "fallback" not in payload


def test_fallback_evidence_remains_review_required_for_rule_extraction():
    html = "<html><body><h1>交易规则</h1><p>Fee schedule.</p></body></html>"
    fetched = _fetch(
        {
            "success": True,
            "data": {
                "markdown": "",
                "html": html,
                "metadata": {"sourceURL": URL, "title": "交易规则"},
            },
        }
    )
    discovered = (
        type(
            "Discovered",
            (),
            {
                "exchange": "SHFE",
                "product": "RB",
                "contract": "SHFE.rb2610",
                "url": URL,
            },
        )(),
    )
    evidence = collect_evidence(
        SOURCE,
        discovered,
        fetcher=lambda url: fetched,
        content_path_builder=lambda digest, suffix: f"documents/{digest}{suffix}",
    )

    assert evidence.documents
    assert evidence.items
    assert all(item.status is RuleStatus.REVIEW_REQUIRED for item in evidence.items)
    assert any("manual rule extraction required" in reason for reason in evidence.review_reasons)


RENDERED_CHALLENGE_MARKDOWN = (
    "当前正在对访问请求进行人机识别检测，请稍等片刻，即将为您加载访问页面\n"
    ".\n.\n.\n确认\n向右滑动填充拼图"
)

TEMPLATE_PLACEHOLDER_MARKDOWN = (
    "| 交易品种 | {{ pageList[0].Product }} |\n"
    "| 最低交易保证金 | {{ pageList[0].MinimumTradeMargin }} |\n"
    "| 涨跌停板幅度 | {{ pageList[0].RangeofPriceLimit }} |"
)


def _fetcher_with_humanlike(monkeypatch, markdown, humanlike_markdown):
    calls = []

    def fake_humanlike_retry(url, *, exchange_homepage=None):
        calls.append(url)
        return source_module._HumanlikeRetryResult(
            markdown=humanlike_markdown,
            attempted=True,
            error=None if humanlike_markdown else "challenge-content",
        )

    fetcher = LocalWebSearchDocumentFetcher(
        executable="websearch",
        runner=lambda command, timeout: _completed(
            {
                "success": True,
                "data": {
                    "markdown": markdown,
                    "metadata": {"sourceURL": URL, "title": "Notice"},
                },
            }
        ),
        now=lambda: NOW,
    )
    monkeypatch.setattr(fetcher, "_humanlike_retry", fake_humanlike_retry)
    monkeypatch.setattr(
        source_module,
        "_rich_scrape_data",
        lambda *args, **kwargs: source_module._RichScrapeResult(
            data=None, attempted=False, error="disabled-in-test"
        ),
    )
    return fetcher, calls


def test_rendered_waf_challenge_markdown_triggers_humanlike_retry(
    monkeypatch: pytest.MonkeyPatch,
):
    fetcher, calls = _fetcher_with_humanlike(
        monkeypatch,
        RENDERED_CHALLENGE_MARKDOWN,
        "关于调整铜等期货相关合约涨跌停板幅度和交易保证金比例的通知",
    )
    document = fetcher.fetch(URL)
    payload = json.loads(document.content)

    assert calls == [URL]
    assert "人机识别检测" not in payload["markdown"]
    assert payload["humanlike_retry_attempted"] is True
    assert payload["metadata"]["fetcher"] == "scrapling_humanlike"


def test_template_placeholder_page_triggers_humanlike_retry(
    monkeypatch: pytest.MonkeyPatch,
):
    fetcher, calls = _fetcher_with_humanlike(
        monkeypatch,
        TEMPLATE_PLACEHOLDER_MARKDOWN,
        "交易品种 中质含硫原油\n最低交易保证金 合约价值的5%",
    )
    document = fetcher.fetch(URL)
    payload = json.loads(document.content)

    assert calls == [URL]
    assert "{{" not in payload["markdown"]
    assert "合约价值的5%" in payload["markdown"]


def test_humanlike_result_that_is_still_a_challenge_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
):
    fetcher, calls = _fetcher_with_humanlike(
        monkeypatch,
        RENDERED_CHALLENGE_MARKDOWN,
        RENDERED_CHALLENGE_MARKDOWN,
    )
    with pytest.raises(DocumentFetchError, match="humanlike fallback failed"):
        fetcher.fetch(URL)

    assert calls == [URL]


CSS_CHALLENGE_MARKDOWN = (
    "WEB 应用防火墙 | #captcha,#js-challenge,#slider,body,html"
    "{width:100%;height:100%;padding:0;margin:0}"
)

JS_OBFUSCATION_MARKDOWN = (
    "window._aMYJPelgGNdHBCHbZMSUTNCYVFeXkCWA = function (){return new Date};"
    "window._OPUTMmUBDeddFdRdSVHNHdYCJEFmNVMW = function(a, b)"
    "{return Date[a].apply(Date, Array.prototype.slice.call(arguments, 1))};"
)


@pytest.mark.parametrize(
    "challenge_markdown",
    [CSS_CHALLENGE_MARKDOWN, JS_OBFUSCATION_MARKDOWN],
)
def test_waf_interstitial_variants_trigger_humanlike_retry(
    monkeypatch: pytest.MonkeyPatch, challenge_markdown: str
):
    fetcher, calls = _fetcher_with_humanlike(
        monkeypatch,
        challenge_markdown,
        "上海国际能源交易中心收费一览表 更新日期：2026年4月27日",
    )
    document = fetcher.fetch(URL)
    payload = json.loads(document.content)

    assert calls == [URL]
    assert "收费一览表" in payload["markdown"]
    assert payload["humanlike_retry_attempted"] is True


def test_fetch_memoizes_repeated_urls_within_one_fetcher(
    monkeypatch: pytest.MonkeyPatch,
):
    fetcher, calls = _fetcher_with_humanlike(
        monkeypatch,
        RENDERED_CHALLENGE_MARKDOWN,
        "第一次拟人抓取得到的正文",
    )
    first = fetcher.fetch(URL)
    second = fetcher.fetch(URL)

    assert calls == [URL]
    assert first is second


def _rich_then_humanlike_fetcher(monkeypatch, initial_markdown, rich_markdown, humanlike_markdown):
    calls = {"rich": 0, "humanlike": 0}

    def runner(command, timeout):
        calls["rich"] += 1
        return _completed(
            {
                "success": True,
                "data": {
                    "markdown": initial_markdown,
                    "metadata": {"sourceURL": URL, "title": "Notice"},
                },
            }
        )

    monkeypatch.setattr(
        source_module.shutil,
        "which",
        lambda name: "firecrawl.cmd" if name == "firecrawl" else name,
    )
    monkeypatch.setattr(
        source_module,
        "_rich_scrape_data",
        lambda *args, **kwargs: (
            calls.__setitem__("rich", calls["rich"] + 1),
            source_module._RichScrapeResult(
                data={"markdown": rich_markdown, "metadata": {"sourceURL": URL}},
                attempted=True,
                error=None,
            ),
        )[1],
    )

    def fake_humanlike_retry(url, *, exchange_homepage=None):
        calls["humanlike"] += 1
        return source_module._HumanlikeRetryResult(
            markdown=humanlike_markdown, attempted=True, error=None
        )

    fetcher = LocalWebSearchDocumentFetcher(
        executable="websearch",
        runner=runner,
        now=lambda: NOW,
    )
    monkeypatch.setattr(fetcher, "_humanlike_retry", fake_humanlike_retry)
    return fetcher, calls


def test_challenge_page_prefers_humanlike_over_rich_renderer(
    monkeypatch: pytest.MonkeyPatch,
):
    fetcher, calls = _rich_then_humanlike_fetcher(
        monkeypatch,
        CSS_CHALLENGE_MARKDOWN,
        "通知列表页（无保证金数值）",
        "经研究决定，自2026年2月5日结算时起，铂、钯期货合约涨跌停板幅度调整为20%，交易保证金标准调整为22%。",
    )
    document = fetcher.fetch(URL)
    payload = json.loads(document.content)

    assert calls["humanlike"] == 1
    assert "交易保证金标准调整为22%" in payload["markdown"]
    assert payload["metadata"]["fetcher"] == "scrapling_humanlike"


def test_challenge_page_keeps_rich_rendering_when_humanlike_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    fetcher, calls = _rich_then_humanlike_fetcher(
        monkeypatch,
        CSS_CHALLENGE_MARKDOWN,
        "通知列表页（代理渲染正文）",
        None,
    )
    document = fetcher.fetch(URL)
    payload = json.loads(document.content)

    assert calls["humanlike"] == 1
    assert "通知列表页" in payload["markdown"]
    assert payload["humanlike_retry_attempted"] is True


def test_rich_rendering_of_empty_page_still_defers_to_humanlike(
    monkeypatch: pytest.MonkeyPatch,
):
    fetcher, calls = _rich_then_humanlike_fetcher(
        monkeypatch,
        "",  # initial scrape returned nothing at all
        "代理渲染的列表页（缺保证金表）",
        "经研究决定，工业硅期货合约投机交易保证金标准调整为10%。",
    )
    document = fetcher.fetch(URL)
    payload = json.loads(document.content)

    assert calls["humanlike"] == 1
    assert "投机交易保证金标准调整为10%" in payload["markdown"]
    assert payload["metadata"]["fetcher"] == "scrapling_humanlike"


def test_transient_humanlike_failure_is_retried_once(
    monkeypatch: pytest.MonkeyPatch,
):
    sleeps = []
    outcomes = iter(
        [
            source_module._HumanlikeRetryResult(
                markdown=None, attempted=True, error="subprocess-exit-1"
            ),
            source_module._HumanlikeRetryResult(
                markdown="重试后的正文", attempted=True, error=None
            ),
        ]
    )

    def fake_subprocess(homepage, url, profile, timeout):
        return next(outcomes)

    monkeypatch.setattr(
        source_module, "_humanlike_subprocess_result", fake_subprocess
    )
    monkeypatch.setattr(source_module, "_load_scrapling_session", lambda: None)
    fetcher = LocalWebSearchDocumentFetcher(
        executable="websearch",
        runner=_forbid_stack,
        now=lambda: NOW,
        sleep_fn=sleeps.append,
        official_domains=("gfex.com.cn",),
        exchange="GFEX",
        exchange_homepage="https://www.gfex.com.cn/",
    )
    result = fetcher._humanlike_retry("https://www.gfex.com.cn/gfex/notice.shtml")

    assert result.markdown == "重试后的正文"
    assert sleeps == [8.0]
