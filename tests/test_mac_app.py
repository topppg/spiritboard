"""The Mac app injects the dashboard's select menu too, because it may show an older dashboard."""

import shutil
import subprocess
from pathlib import Path

import pytest

MENU = Path(__file__).resolve().parents[1] / "src" / "spiritboard" / "static" / "select-menu.js"
NODE = shutil.which("node")
# The page loads the menu and the Mac app injects it again; only the first copy may install.
TWICE = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const listeners = [], sheets = [];
const addEventListener = (type) => listeners.push(type);
const document = {
  addEventListener, documentElement: { dataset: {} }, body: {},
  get adoptedStyleSheets() { return sheets; },
  set adoptedStyleSheets(list) { sheets.splice(0, sheets.length, ...list); },
};
class CSSStyleSheet { replaceSync() {} }
class MutationObserver { observe() {} }
const context = { document, addEventListener, CSSStyleSheet, MutationObserver };
const source = fs.readFileSync(process.argv[1], 'utf8');
vm.runInNewContext(source, context);
vm.runInNewContext(source, context);
assert.equal(sheets.length, 1);
assert.equal(listeners.filter((type) => type === 'mousedown').length, 1);
"""


@pytest.mark.skipif(NODE is None, reason="Node is optional; syntax check skipped")
def test_select_menu_is_valid_javascript() -> None:
    subprocess.run([str(NODE), "--check", str(MENU)], check=True)


@pytest.mark.skipif(NODE is None, reason="Node is optional; menu check skipped")
def test_select_menu_installs_once_when_the_page_and_the_app_both_load_it() -> None:
    subprocess.run([str(NODE), "-e", TWICE, str(MENU)], check=True)
