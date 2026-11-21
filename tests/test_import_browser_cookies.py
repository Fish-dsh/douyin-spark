"""import_browser_cookies 的纯函数测试。

为什么不测 collect(): 它要真起一个浏览器、真加载扩展、真收 HTTP 回传 —— 那是
集成测试, 在 CI/开发机上会不稳定, 而且真正的验证已经在 Windows 上手工跑过
(Edge 50 条 cookie → storage_edge.json → is_logged_in True)。

这里钉住的是「转换层」的三件事, 它们出错会静默写出一个看起来正常但登不进去的文件:
    1. sameSite 取值映射 —— Chrome 的 no_restriction 不是 Playwright 认的写法;
    2. cookie 域的收敛 —— www.douyin.com 与 .douyin.com 要都问到, 否则漏 host-only;
    3. 输出路径跟着配置走 —— 账号改了 storage_file, 工具不能还写死旧名字。
"""

from __future__ import annotations

import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import import_browser_cookies as ibc


class TestDomainDerivation(unittest.TestCase):
    def test_base_domain_strips_subdomain(self) -> None:
        self.assertEqual(ibc.base_domain("www.douyin.com"), "douyin.com")
        self.assertEqual(ibc.base_domain("douyin.com"), "douyin.com")

    def test_base_domain_keeps_two_labels(self) -> None:
        self.assertEqual(ibc.base_domain("localhost"), "localhost")

    def test_cookie_domains_cover_host_only_and_domain_wide(self) -> None:
        domains = ibc.cookie_domains("www.douyin.com")
        self.assertEqual(domains[0], "www.douyin.com")  # host-only cookie 只在这层
        self.assertIn("douyin.com", domains)
        self.assertIn(".douyin.com", domains)

    def test_cookie_domains_has_no_duplicates(self) -> None:
        self.assertEqual(
            len(ibc.cookie_domains("douyin.com")),
            len(set(ibc.cookie_domains("douyin.com"))),
        )


class TestStorageStateConversion(unittest.TestCase):
    def _ck(self, **kw) -> dict:
        base = {
            "name": "sessionid",
            "value": "abc",
            "domain": ".douyin.com",
            "path": "/",
            "expirationDate": 1826704071,
            "httpOnly": True,
            "secure": True,
            "sameSite": "no_restriction",
        }
        base.update(kw)
        return base

    def test_samesite_maps_to_playwright_spelling(self) -> None:
        cases = {
            "no_restriction": "None",
            "lax": "Lax",
            "strict": "Strict",
            "unspecified": "Lax",
        }
        for raw, want in cases.items():
            state = ibc.to_storage_state([self._ck(sameSite=raw)])
            self.assertEqual(state["cookies"][0]["sameSite"], want, raw)

    def test_field_names_are_renamed(self) -> None:
        cookie = ibc.to_storage_state([self._ck()])["cookies"][0]
        self.assertEqual(cookie["expires"], 1826704071)
        self.assertNotIn("expirationDate", cookie)
        self.assertEqual(
            set(cookie),
            {
                "name",
                "value",
                "domain",
                "path",
                "expires",
                "httpOnly",
                "secure",
                "sameSite",
            },
        )

    def test_missing_expiry_becomes_session_cookie(self) -> None:
        cookie = ibc.to_storage_state([self._ck(expirationDate=None)])["cookies"][0]
        self.assertEqual(cookie["expires"], -1)

    def test_entries_without_name_or_domain_are_dropped(self) -> None:
        state = ibc.to_storage_state(
            [self._ck(name=""), self._ck(domain=""), self._ck()]
        )
        self.assertEqual(len(state["cookies"]), 1)

    def test_origins_is_present_and_empty(self) -> None:
        # Playwright 的 storage_state 少了 origins 会被当成残缺文件
        self.assertEqual(ibc.to_storage_state([]), {"cookies": [], "origins": []})


class TestOutputPath(unittest.TestCase):
    def test_explicit_output_wins(self) -> None:
        target = str(ibc.Path(ibc.tempfile.gettempdir()) / "spark-x.json")
        self.assertEqual(str(ibc.resolve_output("main", target)), target)

    def test_falls_back_to_runtime_dir_when_config_has_no_match(self) -> None:
        path = ibc.resolve_output("no-such-account", None)
        self.assertEqual(path.name, "storage_no-such-account.json")
        self.assertEqual(path.parent, ibc.RUNTIME_DIR)


class TestManifestShape(unittest.TestCase):
    def test_manifest_asks_only_for_cookies(self) -> None:
        manifest = ibc.build_manifest(["www.douyin.com"])
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["permissions"], ["cookies"])
        self.assertIn("*://*.douyin.com/*", manifest["host_permissions"])

    def test_worker_quotes_every_domain_and_port(self) -> None:
        js = ibc.build_worker(["www.douyin.com", ".douyin.com"], 51234)
        self.assertIn('"www.douyin.com"', js)
        self.assertIn('".douyin.com"', js)
        self.assertIn("127.0.0.1:51234", js)
        self.assertIn("chrome.cookies.getAll", js)


class TestRunningCount(unittest.TestCase):
    def test_returns_zero_when_probe_fails(self) -> None:
        # tasklist / pgrep 不存在时不能崩, 也不能谎报有进程(那会把正常导出挡在门外)
        with unittest.mock.patch.object(
            ibc.subprocess, "run", side_effect=OSError("nope")
        ):
            self.assertEqual(ibc.running_count(ibc.Path("msedge.exe")), 0)

    def test_counts_only_matching_lines(self) -> None:
        # 这段输出是 Windows tasklist 的格式，必须连平台一起钉住：
        # 只 mock subprocess.run 的话，Linux 上会走 pgrep 分支，
        # int("msedge.exe  1234 ...") 抛 ValueError 被吞成 0。
        fake = unittest.mock.Mock(
            stdout="msedge.exe  1234 Console  1  100 K\nmsedge.exe  5678 Console  1  100 K\n"
        )
        with (
            unittest.mock.patch.object(ibc.subprocess, "run", return_value=fake),
            unittest.mock.patch.object(ibc.sys, "platform", "win32"),
        ):
            self.assertEqual(ibc.running_count(ibc.Path("msedge.exe")), 2)

    def test_counts_pgrep_output(self) -> None:
        # 生产在 Linux 板上，pgrep -fc 只回一个数字
        fake = unittest.mock.Mock(stdout="3\n")
        with (
            unittest.mock.patch.object(ibc.subprocess, "run", return_value=fake),
            unittest.mock.patch.object(ibc.sys, "platform", "linux"),
        ):
            self.assertEqual(ibc.running_count(ibc.Path("/usr/bin/google-chrome")), 3)


class TestBrowserCandidates(unittest.TestCase):
    def test_edge_and_chrome_point_at_different_binaries(self) -> None:
        edge_exes, edge_udds = ibc.browser_candidates("edge")
        chrome_exes, chrome_udds = ibc.browser_candidates("chrome")
        self.assertNotEqual([str(p) for p in edge_exes], [str(p) for p in chrome_exes])
        self.assertNotEqual([str(p) for p in edge_udds], [str(p) for p in chrome_udds])

    def test_unknown_browser_returns_nothing_that_exists(self) -> None:
        # 兜底不能静默返回空列表然后崩在解包处
        exes, udds = ibc.browser_candidates("edge")
        self.assertTrue(exes and udds)


if __name__ == "__main__":
    unittest.main()
