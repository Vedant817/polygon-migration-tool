"""Allow-list HTML sanitiser for problem content fetched from Polygon.

The statement, input format, output format, notes, constraints and editorial are
extracted verbatim from Polygon's ``problem.html``. Rendering them escaped made
the page show literal markup - every screenshot read
``<p>You are given two integers $$$a$$$ and $$$b$$$.</p>`` as visible text.
Rendering them with ``|safe`` instead would execute whatever the problem
contains, and this content is remote.

So the HTML is filtered here and only the result is marked safe. The approach is
an allow-list: anything not explicitly permitted is removed, and elements that
can execute or embed are removed together with their contents. Unknown but
harmless elements are unwrapped, so their text survives.

The database keeps the original HTML. Only what is sent to the browser is
filtered, so nothing is lost for the migration itself.
"""

from bs4 import BeautifulSoup

# Formatting that carries meaning in a problem statement.
ALLOWED_TAGS = frozenset({
    "p", "br", "hr", "blockquote",
    "b", "strong", "i", "em", "u", "s", "strike", "small", "sub", "sup",
    "code", "pre", "kbd", "samp", "var",
    "ul", "ol", "li", "dl", "dt", "dd",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    "span", "div", "a",
})

# Only elements that can execute script or pull in an external resource are
# removed together with their contents. Everything else - <form>, <button>,
# <marquee>, <canvas>, <audio> and friends - is merely unwrapped below, so its
# text survives. Dropping those would silently delete real sentences.
DROPPED_WITH_CONTENT = frozenset({
    "script", "style", "iframe", "frame", "frameset", "object", "embed",
    "applet", "link", "meta", "base", "noscript", "template",
    "svg", "math",
})

# href on <a> is the one attribute that can carry a URL, and URLs can carry
# javascript:. Everything else is stripped.
ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "ol": {"start", "type"},
}

SAFE_URL_SCHEMES = frozenset({"http", "https", "mailto"})


def _safe_href(value):
    """Return *value* if it is a relative path or an allowed scheme, else None.

    Entity-encoded and whitespace-padded values are normalised first, so
    ``java&#115;cript:alert(1)`` and ``  javascript:alert(1)  `` are both
    rejected rather than slipping past a naive prefix check.
    """
    if value is None:
        return None
    candidate = value.strip().replace("\x00", "")
    # A scheme cannot contain whitespace or these characters; if the first
    # character is not a scheme character there is no scheme at all.
    if ":" not in candidate:
        return candidate
    head = candidate.split(":", 1)[0].strip().lower()
    if not head:
        # "://evil" or a leading colon - not a scheme we trust.
        return None
    if not all(ch.isalnum() or ch in "+-." for ch in head):
        return None
    return candidate if head in SAFE_URL_SCHEMES else None


def sanitize_html(raw):
    """Return *raw* with everything unsafe removed.

    ``None`` and blank input return an empty string so a template can render the
    result unconditionally.
    """
    if not raw:
        return ""
    text = raw if isinstance(raw, str) else str(raw)

    soup = BeautifulSoup(text, "html.parser")

    for element in soup.find_all(list(DROPPED_WITH_CONTENT)):
        element.decompose()

    for element in soup.find_all(True):
        if element.name not in ALLOWED_TAGS:
            # Keep the words, drop the tag.
            element.unwrap()

    for element in soup.find_all(True):
        if element.decomposed:
            continue
        permitted = ALLOWED_ATTRS.get(element.name, set())
        for attribute in list(element.attrs):
            if attribute not in permitted:
                del element[attribute]
        if element.name == "a" and element.get("href") is not None:
            href = _safe_href(element.get("href"))
            if href is None:
                del element["href"]
            elif href != element.get("href"):
                element["href"] = href

    return str(soup)
