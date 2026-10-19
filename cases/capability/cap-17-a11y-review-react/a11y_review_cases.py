"""Reference cases for cap-17-a11y-review-react: findings.json against the planted violations.

A finding matches a planted violation when its file and rule are equal and its line
is in the violation's line range (the element plus a little slack). Footer.jsx and
LoginForm.jsx are correct on purpose (decorative image with alt="", icon link with
aria-label, labelled inputs) and must not be reported.
"""

from agenteval.suites.cases import Case

SETUP = """
import json
from pathlib import Path

PLANTED = {
    "lang": ("public/index.html", "html-lang", range(1, 4)),
    "contrast": ("src/styles.css", "color-contrast", [*range(3, 6), *range(17, 22)]),
    "focus": ("src/styles.css", "focus-visible", range(23, 28)),
    "avatar_alt": ("src/components/Avatar.jsx", "img-alt", range(3, 6)),
    "search_label": ("src/components/SearchBar.jsx", "form-label", range(5, 12)),
    "search_button": ("src/components/SearchBar.jsx", "button-name", range(11, 15)),
    "card_click": ("src/components/ProductCard.jsx", "click-no-keyboard", range(3, 6)),
    "card_link": ("src/components/ProductCard.jsx", "link-name", range(9, 12)),
    "heading_skip": ("src/components/ProductPage.jsx", "heading-order", range(8, 11)),
    "autoplay": ("src/components/PromoBanner.jsx", "media-autoplay", range(4, 7)),
    "tabindex": ("src/components/CartDrawer.jsx", "tabindex-positive", range(11, 14)),
    "hidden_focusable": ("src/components/CartDrawer.jsx", "aria-hidden-focusable", range(2, 17)),
}


def norm_file(value):
    return str(value).strip().replace("\\\\", "/").removeprefix("./")


FINDINGS = [
    (norm_file(f["file"]), str(f["rule"]).strip().lower(), int(f["line"]))
    for f in json.loads(Path(WORKSPACE, "findings.json").read_text())
]


def hits(planted):
    file, rule, lines = planted
    return [f for f in FINDINGS if f[0] == file and f[1] == rule and f[2] in lines]


def found(key):
    return bool(hits(PLANTED[key]))


def in_file(file):
    return len([f for f in FINDINGS if f[0] == file])


def unmatched():
    matched = {f for planted in PLANTED.values() for f in hits(planted)}
    return len([f for f in FINDINGS if f not in matched])
"""

CASES = [
    *[
        Case(f"found_{key}", f"found({key!r})", True)
        for key in (
            "lang",
            "contrast",
            "focus",
            "avatar_alt",
            "search_label",
            "search_button",
            "card_click",
            "card_link",
            "heading_skip",
            "autoplay",
            "tabindex",
            "hidden_focusable",
        )
    ],
    Case("decoy_login_form_not_reported", "in_file('src/components/LoginForm.jsx')", 0),
    Case("decoy_footer_not_reported", "in_file('src/components/Footer.jsx')", 0),
    Case("at_most_three_other_findings", "unmatched() <= 3", True),
]
