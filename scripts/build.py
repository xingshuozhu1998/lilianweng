#!/usr/bin/env python3
"""从原站静态 HTML 提取翻译段落，再按缓存生成共享资源的中英文网站。

只使用 Python 标准库。extract 不生成假译文；build 的未译段落保留原文，
并在文章顶部及清单明确标记。行内标签、公式和代码必须原样恢复。
"""
import argparse
import hashlib
import html
import json
import re
import shutil
import xml.etree.ElementTree as ET
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://lilianweng.github.io"
SITE = "https://xingshuozhu1998.github.io/lilianweng"
VOID = set("area base br col embed hr img input link meta param source track wbr".split())
SKIP = set("script style pre code svg textarea noscript".split())
BLOCK = set("p li h1 h2 h3 h4 h5 h6 td th figcaption dd dt".split())
BLOCK_DESCENDANTS = BLOCK | set("div ul ol table section article blockquote pre dl".split())
MATH = re.compile(r"(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$|(?<![\\$])\$(?!\$)(?:\\.|[^$])*?(?<!\\)\$(?!\$)|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]")
TOKEN = re.compile(r"__LW_(?:TAG|CODE|MATH)_\d+__")
ATTR = re.compile(r"\b([\w:-]+)(\s*=\s*)([\"'])(.*?)\3", re.S)


class Document(HTMLParser):
    """记录源码位置，编辑时不重新序列化 HTML，避免改变原格式与公式。"""
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source, self.stack, self.nodes, self.groups, self.tags, self.hidden_anchors = source, [], [], [], [], []
        self.lines = [0]
        self.lines.extend(m.end() for m in re.finditer("\n", source))
        self.feed(source)

    def pos(self):
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def handle_starttag(self, tag, attrs):
        start = self.pos()
        raw = self.get_starttag_text()
        self.tags.append((start, start + len(raw), tag, dict(attrs)))
        if tag in BLOCK_DESCENDANTS:
            for item in self.stack:
                if item[0] in BLOCK:
                    item[4] = True
        if tag not in VOID:
            self.stack.append([tag, start, start + len(raw), dict(attrs), False])

    def handle_startendtag(self, tag, attrs):
        raw = self.get_starttag_text()
        self.tags.append((self.pos(), self.pos() + len(raw), tag, dict(attrs)))

    def handle_endtag(self, tag):
        for idx in range(len(self.stack) - 1, -1, -1):
            if self.stack[idx][0] == tag:
                item = self.stack[idx]
                ancestors = self.stack[:idx]
                if tag == "a" and "hidden" in item[3]:
                    self.hidden_anchors.append((item[1], self.source.index(">", self.pos()) + 1))
                if tag in BLOCK and not item[4] and not any(a[0] in SKIP for a in ancestors):
                    self.groups.append((item[2], self.pos(), tag))
                del self.stack[idx:]
                break

    def data(self, length):
        start = self.pos()
        allowed = any(a[0] == "body" for a in self.stack) and not any(a[0] in SKIP for a in self.stack)
        title = bool(self.stack and self.stack[-1][0] == "title")
        if allowed or title:
            if self.nodes and self.nodes[-1][1] == start:
                self.nodes[-1] = (self.nodes[-1][0], start + length)
            else:
                self.nodes.append((start, start + length))

    def handle_data(self, data):
        self.data(len(data))

    def handle_entityref(self, name):
        start = self.pos()
        self.data(len(name) + 1 + int(self.source[start + len(name) + 1:start + len(name) + 2] == ";"))

    def handle_charref(self, name):
        start = self.pos()
        self.data(len(name) + 2 + int(self.source[start + len(name) + 2:start + len(name) + 3] == ";"))


def protect(raw):
    placeholders, counts = [], defaultdict(int)

    def replace(kind, original):
        token = f"__LW_{kind}_{counts[kind]}__"
        counts[kind] += 1
        placeholders.append({"token": token, "raw": original, "kind": kind.lower()})
        return token

    # 读取真实 hidden 属性；href、aria-label 或 class 的文字都不能冒充该属性。
    ranges = Document(raw).hidden_anchors
    ranges += [match.span() for match in re.finditer(r"<(code|pre)\b[^>]*>[\s\S]*?</\1\s*>", raw, re.I)]
    parts, previous = [], 0
    for start, end in sorted(ranges, key=lambda value: (value[0], -value[1])):
        if start < previous:
            continue
        parts.extend([raw[previous:start], replace("CODE", raw[start:end])])
        previous = end
    raw = "".join(parts) + raw[previous:]
    raw = MATH.sub(lambda m: replace("MATH", m.group()), raw)
    raw = re.sub(r"<!--[\s\S]*?-->|</?[A-Za-z][^>]*>", lambda m: replace("TAG", m.group()), raw)
    return html.unescape(raw), placeholders


def segment(raw, kind):
    leading = len(raw) - len(raw.lstrip())
    trailing = len(raw.rstrip())
    text = raw[leading:trailing]
    protected, placeholders = protect(text)
    readable = TOKEN.sub("", protected)
    if not re.search(r"[A-Za-z]{2}", readable) or re.fullmatch(r"https?://\S+", readable.strip()):
        return None
    return {"id": hashlib.sha256(text.encode()).hexdigest()[:20],
            "text": html.unescape(re.sub(r"<[^>]*>", "", text)),
            "protected": protected, "placeholders": placeholders, "kind": kind}, leading, trailing


def chunks(source):
    doc = Document(source)
    groups = sorted(doc.groups)
    ranges = [(a, b, "block") for a, b, _ in groups]
    # 标题、导航、目录和裸文本只在未被段落覆盖时单独翻译。
    for start, end in doc.nodes:
        if not any(a <= start and end <= b for a, b, _ in groups):
            ranges.append((start, end, "text"))
    for start, end, tag, attrs in doc.tags:
        raw = source[start:end]
        for match in ATTR.finditer(raw):
            name = match[1].lower()
            # 行内标签作为父段落占位符整体保护，其属性不得再次按旧源码位置编辑。
            # 因此段落中的英文 alt/title 保留原样；可见图注仍作为正文翻译。
            attr_start, attr_end = start + match.start(4), start + match.end(4)
            if any(a <= attr_start and attr_end <= b for a, b, _ in groups):
                continue
            if name in {"alt", "title", "aria-label", "placeholder"} or (
                    tag == "meta" and name == "content" and
                    (attrs.get("name") in {"description", "twitter:title", "twitter:description"} or
                     attrs.get("property") in {"og:title", "og:description"})):
                ranges.append((attr_start, attr_end, "attribute"))
    result = []
    for start, end, kind in sorted(ranges):
        parsed = segment(source[start:end], kind)
        if parsed:
            item, leading, trailing = parsed
            result.append((start + leading, start + trailing, item))
    return result


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def extract(upstream, destination):
    segments, posts = {}, []
    for path in sorted(upstream.rglob("*.html")):
        relative = path.relative_to(upstream).as_posix()
        source = path.read_text()
        ids = []
        for _, _, item in chunks(source):
            ids.append(item["id"])
            if item["id"] not in segments:
                segments[item["id"]] = {**item, "paths": []}
            if relative not in segments[item["id"]]["paths"]:
                segments[item["id"]]["paths"].append(relative)
        if re.fullmatch(r"posts/\d{4}-\d{2}-\d{2}-[^/]+/index.html", relative):
            title = html.unescape(re.search(r"<h1\b[^>]*>([\s\S]*?)</h1>", source)[1]).strip()
            posts.append({"slug": path.parent.name, "path": relative, "title": title,
                          "original_url": ORIGIN + "/" + relative[:-10], "segments": list(dict.fromkeys(ids)),
                          "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    write_json(destination, {"version": 1, "origin": ORIGIN, "site": SITE,
                             "segments": list(segments.values()), "posts": posts})
    print(f"提取完成：{len(posts)} 篇文章，{len(segments)} 个去重翻译段落 -> {destination}")


def restore(item, translated):
    expected = TOKEN.findall(item["protected"])
    actual = TOKEN.findall(translated)
    if expected != actual:
        raise ValueError(f"占位符数量或顺序变化：{item['id']}: {expected} != {actual}")
    rendered = html.escape(translated, quote=item["kind"] == "attribute")
    # 后保护的注释或标签可能包住先保护的公式，先恢复外层再恢复内层。
    for placeholder in reversed(item["placeholders"]):
        rendered = rendered.replace(placeholder["token"], placeholder["raw"])
    return rendered


def rewrite_urls(source, prefix, upstream, relative):
    # 页面链接按语言分流；图片、字体、样式和脚本统一保留根路径，只复制一份。
    def local_url(url):
        if url.startswith(ORIGIN):
            url = url[len(ORIGIN):] or "/"
        elif url.startswith("#") or url.startswith("//") or urlsplit(url).scheme:
            return url
        elif not url.startswith("/"):
            # 英文页面只复制 HTML，相对图片地址必须仍解析到原共享资源目录。
            url = urljoin(ORIGIN + "/" + relative, url)[len(ORIGIN):]
        path = urlsplit(html.unescape(url)).path
        target = upstream / path.lstrip("/")
        is_page = path.endswith((".html", ".xml", ".json")) or path == "/" or (
            target.is_dir() and (target / "index.html").exists())
        if is_page and prefix:
            url = prefix + url
        return SITE + url

    def attribute(match):
        value = match[4]
        name = match[1].lower()
        if name in {"href", "src", "action"} or (name == "content" and value.startswith(ORIGIN)):
            value = local_url(value)
        if name in {"href", "content"}:
            if name == "content" and value.startswith("0; url=" + ORIGIN):
                value = "0; url=" + local_url(value[len("0; url="):])
            value = re.sub(re.escape(quote(ORIGIN, safe="")), quote(SITE + prefix, safe=""), value, flags=re.I)
        return match[1] + match[2] + match[3] + value + match[3]

    # 只编辑实际 HTML 标签；若对整个源码匹配属性，会误改代码块中的示例代码。
    for start, end, _, _ in reversed(Document(source).tags):
        source = source[:start] + ATTR.sub(attribute, source[start:end]) + source[end:]
    # 原作者网址在正文 BibTeX（文献引用格式）及代码中有来源含义，保持原样。
    # 只有脚本中的重定向和结构化站点地址需要改为本站；数学配置不含原站地址。
    source = re.sub(r'<script\b[^>]*>[\s\S]*?</script>',
                    lambda m: m.group().replace(ORIGIN, SITE + prefix), source)
    source = re.sub(r'(<title>)' + re.escape(ORIGIN) + r'([^<]*)(</title>)',
                    lambda m: m[1] + SITE + prefix + m[2] + m[3], source)
    return source


def rewrite_metadata(source, prefix, title_map):
    """机器译文的结构化摘要与正文保持一致，同时保留作者身份。"""
    title = text_of(source, "post-title")
    body = text_of(source, "post-content")
    description = re.search(r'<meta name="description" content="([\s\S]*?)"', source)

    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key == "author" and isinstance(child, dict):
                    child["name"] = "Lilian Weng"
                    if "url" in child:
                        child["url"] = ORIGIN + "/"
                else:
                    visit(child)
                if not prefix and key in {"name", "headline"} and isinstance(child, str) and child in title_map:
                    value[key] = title_map[child]
            if value.get("@type") == "BlogPosting":
                value.update({"headline": title, "name": title, "articleBody": body,
                              "inLanguage": "en" if prefix else "zh-CN"})
                if description:
                    value["description"] = html.unescape(description[1]).strip()

    def replace(match):
        value = json.loads(match[2])
        visit(value)
        return match[1] + json.dumps(value, ensure_ascii=False).replace("</", "<\\/") + match[3]

    return re.sub(r'(<script type="application/ld\+json">)([\s\S]*?)(</script>)', replace, source)


def text_of(source, class_name):
    match = re.search(r'<(?:div|h1)\b[^>]*class="' + re.escape(class_name) + r'"[^>]*>', source)
    if not match:
        return ""
    doc = Document(source)
    opening = next((tag for tag in doc.tags if tag[0] == match.start()), None)
    # 正文终止于文章 footer；只用于搜索文字，不影响原页面布局。
    end = source.find('<footer class="post-footer">', opening[1]) if class_name == "post-content" else source.find("</h1>", opening[1])
    fragment = source[opening[1]:end if end >= 0 else len(source)]
    fragment = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", fragment)
    return html.unescape(re.sub(r"<[^>]*>", " ", fragment)).strip()


def explain_first_terms(source, terms):
    """以文章为单位解释保留英文术语；目录和属性不占用正文的首次出现。

    只编辑标题和正文中的纯文本。原代码节点被解析器排除，公式先以占位符
    保护，因此术语规范化不会改变代码、公式、HTML 标签和图片属性。
    """
    if not terms:
        return source

    class ArticleText(Document):
        def __init__(self, document):
            self.references = False
            super().__init__(document)

        def handle_starttag(self, tag, attrs):
            inside_body = any("post-content" in (item[3].get("class") or "").split() for item in self.stack)
            if inside_body and tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                self.references = bool(re.search(r"references?|citation|papers-mentioned|blog-posts-mentioned|useful-resources|interesting-blogs", dict(attrs).get("id", ""), re.I))
            super().handle_starttag(tag, attrs)

        def data(self, length):
            selected = any(set((item[3].get("class") or "").split()) & {"post-title", "post-content"} for item in self.stack)
            if not selected or self.references or any(item[0] in SKIP for item in self.stack):
                return
            start = self.pos()
            if self.nodes and self.nodes[-1][1] == start:
                self.nodes[-1] = (self.nodes[-1][0], start + length)
            else:
                self.nodes.append((start, start + length))

    lookup = {key.casefold(): value for key, value in terms.items()}
    concepts = {key: key.replace(" ", "").replace("-", "") for key in lookup}
    concept_meanings = {concepts[key]: meaning for key, meaning in lookup.items()}
    for key, concept in list(concepts.items()):
        if concept.endswith("s") and concept_meanings.get(concept[:-1]) == lookup[key]:
            concepts[key] = concept[:-1]
    # 大写模型缩写区分大小写，避免给普通词 made/nice 加上模型解释。
    alternatives = [re.escape(key) if key.isupper() else "(?i:" + re.escape(key) + ")" for key in sorted(terms, key=len, reverse=True)]
    expression = re.compile(r"(?<![A-Za-z0-9_-])(?:" + "|".join(alternatives) + r")(?![A-Za-z0-9_-])")
    seen, edits, pending, previous_end = set(), [], None, None
    for start, end in ArticleText(source).nodes:
        raw = source[start:end]
        protected, placeholders = protect(raw)
        original_protected = protected
        # 模型可能把括释放在强调/链接的闭标签外：<b>term</b>（释义）。
        # 前一文本节点已经规范化该词，因此只移除紧随行内闭标签的同一括释。
        if pending and previous_end is not None and re.fullmatch(r"(?:</(?:a|b|i|em|strong|span|mark|small|sup|sub|u|s|del)>)*", source[previous_end:start], re.I):
            separated = re.match(r"[ \t]*(?:（" + re.escape(pending) + r"）|\(" + re.escape(pending) + r"\))", protected)
            if separated:
                protected = protected[separated.end():]
                if re.match(r"[A-Za-z]", protected):
                    protected = " " + protected
        pending = None
        result, previous = [], 0
        for match in expression.finditer(protected):
            # 释义必须是词表约定的同一释义；不删除作者原有的其他括号内容。
            if match.start() < previous:
                continue
            term, key = match.group(), match.group().casefold()
            explanation = lookup[key]
            concept = concepts[key]
            annotation = re.match(r"[ \t]*(?:（" + re.escape(explanation) + r"）|\(" + re.escape(explanation) + r"\))", protected[match.end():])
            result.append(protected[previous:match.start()])
            repeated = concept in seen
            result.append(term if repeated else term + "（" + explanation + "）")
            seen.add(concept)
            previous = match.end() + (annotation.end() if annotation else 0)
            # 去除括释后仍保留英文词边界，例如 off-policy（释义）RL。
            if repeated and annotation and re.match(r"[A-Za-z]", protected[previous:]):
                result.append(" ")
            if not protected[previous:].strip():
                pending = explanation
        result.append(protected[previous:])
        normalized = "".join(result)
        # 删除括释后，行内链接/强调标签两侧的英文仍须分词，避免出现 APIplayground。
        if normalized != original_protected and re.search(r"[A-Za-z]$", normalized) and re.match(r"(?:</?(?:a|b|i|em|strong|span|mark|small|sup|sub|u|s|del)\b[^>]*>)+[A-Za-z]", source[end:], re.I):
            normalized += " "
        if normalized != original_protected:
            item = {"id": "first-occurrence", "protected": original_protected, "placeholders": placeholders, "kind": "text"}
            edits.append((start, end, restore(item, normalized)))
        previous_end = end
    for start, end, replacement in reversed(edits):
        source = source[:start] + replacement + source[end:]
    return source


def build(upstream, output, source_path, cache_path):
    data = json.loads(source_path.read_text())
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    posts = {p["path"]: p for p in data["posts"]}
    titles_path = ROOT / "translations/titles.json"
    titles = json.loads(titles_path.read_text()) if titles_path.exists() else {}
    terms_path = ROOT / "translations/english_terms.json"
    terms = json.loads(terms_path.read_text()) if terms_path.exists() else {}
    title_map = {post["title"]: titles[post["slug"]] for post in data["posts"] if post["slug"] in titles}
    # 经过审核的标题优先于机器缓存，同时用于正文标题、列表、目录、搜索与元信息。
    for item in data["segments"]:
        original_title = item["protected"].strip()
        if original_title in title_map:
            cache[item["id"]] = title_map[original_title]
        elif original_title.endswith(" | Lil'Log") and original_title[:-len(" | Lil'Log")] in title_map:
            cache[item["id"]] = title_map[original_title[:-len(" | Lil'Log")]] + " | Lil’Log 中文学习镜像"
    translated, missing = 0, 0
    output.mkdir(parents=True, exist_ok=True)
    # 不删除用户目录，只覆盖本次输出。非页面资源位于中英文共用的根目录。
    for path in upstream.rglob("*"):
        if not path.is_file() or ".git" in path.parts or path.suffix in {".html", ".xml", ".json"} or path.name in {"robots.txt", ".gitignore"}:
            continue
        target = output / path.relative_to(upstream)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    # 标签等原站 RSS 仍供导航使用；根 RSS 在下面由对应语言正文重建。
    for path in upstream.rglob("*.xml"):
        for prefix in ["", "/en"]:
            target = output / prefix.strip("/") / path.relative_to(upstream)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path.read_text().replace(ORIGIN, SITE + prefix))
    indexes = {"": [], "/en": []}
    manifest = []
    for path in sorted(upstream.rglob("*.html")):
        relative = path.relative_to(upstream).as_posix()
        original = path.read_text()
        # 原文明确区分两种同名 DPG；按文章解释缩写，避免全局词表误导。
        term_overrides = {}
        if "Distributional Policy Gradient" in original:
            term_overrides["DPG"] = "分布策略梯度，区别于确定性策略梯度"
        # EM 在问答评估、运输距离与参数估计中含义不同，不能统一解释成期望最大化。
        if "REALM++" in original:
            term_overrides["EM"] = "完全匹配"
        elif "Earth Mover" in original:
            term_overrides["EM"] = "推土机距离"
        # 视频文章的 SVD 指 Stable Video Diffusion，其他文章仍指矩阵的奇异值分解。
        if "Stable Video Diffusion" in original:
            term_overrides["SVD"] = "稳定视频扩散模型"
        page_terms = {**terms, **term_overrides}
        page_chunks = chunks(original)
        page_missing = [item["id"] for _, _, item in page_chunks if item["id"] not in cache]
        for prefix in ["", "/en"]:
            rendered = original
            if not prefix:
                for start, end, item in reversed(page_chunks):
                    if item["id"] in cache:
                        rendered = rendered[:start] + restore(item, cache[item["id"]]) + rendered[end:]
                        translated += 1
                    else:
                        missing += 1
                rendered = rendered.replace('lang="en-us"', 'lang="zh-CN"')
                if relative in posts:
                    rendered = explain_first_terms(rendered, page_terms)
            # 移除原作者 Google Analytics（访问统计），避免镜像访问进入原作者账户。
            rendered = re.sub(r'<script\b[^>]*src="https://www.googletagmanager.com/[^>]*></script>\s*<script>[\s\S]*?</script>', "", rendered)
            rendered = rewrite_urls(rendered, prefix, upstream, relative)
            rendered = rewrite_metadata(rendered, prefix, title_map)
            page_url = "/" + relative.removesuffix("index.html")
            note = (f'<aside class="translation-note" style="margin:16px 0;padding:12px;border:1px solid var(--border);border-radius:6px">'
                    f'作者：Lilian Weng · <a href="{ORIGIN + page_url}">作者原站</a> · '
                    f'<a href="{SITE + (page_url if prefix else "/en" + page_url)}">{"中文译文" if prefix else "本站英文原文"}</a>')
            if prefix:
                note += ' · English mirror; original author: Lilian Weng.'
            elif page_missing:
                note += f' · 翻译进行中：本页仍有 {len(page_missing)} 个未译片段，保留英文供对照。'
            else:
                note += ' · GPT-6.1-sol 译稿；公式、代码及图片按原文保留。'
            note += '</aside>'
            rendered = rendered.replace('<main class="main">', '<main class="main">\n' + note, 1)
            # 为双方页面提供明确的语言对应关系，不把中文页声明为英文 hreflang。
            rendered = re.sub(r'<link rel="alternate" hreflang="en"[^>]*>', "", rendered)
            alternate = (f'<link rel="alternate" hreflang="zh-CN" href="{SITE + page_url}">\n'
                         f'<link rel="alternate" hreflang="en" href="{SITE + "/en" + page_url}">\n')
            rendered = rendered.replace('</head>', alternate + '</head>')
            target = output / prefix.strip("/") / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered)
            if relative in posts:
                title = text_of(rendered, "post-title")
                content = text_of(rendered, "post-content")
                indexes[prefix].append({"title": title, "content": content, "permalink": SITE + prefix + page_url})
        if relative in posts:
            post = posts[relative]
            manifest.append({**post, "chinese_url": SITE + page_url, "english_url": SITE + "/en" + page_url,
                             "translated_segments": len(page_chunks) - len(page_missing), "total_segments": len(page_chunks),
                             "missing_segments": list(dict.fromkeys(page_missing)),
                             "term_explanation_overrides": term_overrides,
                             "status": "gpt_translated" if not page_missing else "incomplete"})
    for prefix, index in indexes.items():
        base = output / prefix.strip("/")
        write_json(base / "index.json", index)
        urls = [SITE + prefix + "/" + p.relative_to(upstream).as_posix().removesuffix("index.html") for p in sorted(upstream.rglob("index.html"))]
        sitemap = '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(f'<url><loc>{html.escape(u)}</loc></url>' for u in urls) + '</urlset>\n'
        (base / "sitemap.xml").write_text(sitemap)
        channel_title = "Lil’Log English mirror" if prefix else "Lil’Log 中文学习镜像"
        channel_description = "English mirror of Lilian Weng’s blog." if prefix else "Lilian Weng 文章学习镜像；机器译文需审校。"
        rss = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
               f'<title>{channel_title}</title><link>{SITE + prefix}/</link><description>{channel_description}</description>' +
               ''.join(f'<item><title>{html.escape(i["title"])}</title><link>{i["permalink"]}</link><guid>{i["permalink"]}</guid><description>{html.escape(i["content"][:500])}</description></item>' for i in index) + '</channel></rss>\n')
        (base / "index.xml").write_text(rss)
        # 分类/标签 RSS 使用同一译文摘要，避免中文站的订阅器仍只看到原文。
        index_by_path = {urlsplit(row["permalink"]).path.removeprefix(prefix): row for row in index}
        for original_feed in upstream.rglob("index.xml"):
            if original_feed == upstream / "index.xml":
                continue
            tree = ET.parse(original_feed)
            channel = tree.getroot().find("channel")
            if channel is None:
                continue
            for node in channel.iter():
                if node.text and ORIGIN in node.text:
                    node.text = node.text.replace(ORIGIN, SITE + prefix)
                for key, value in node.attrib.items():
                    if ORIGIN in value:
                        node.set(key, value.replace(ORIGIN, SITE + prefix))
            channel.find("title").text = channel_title + " · " + original_feed.parent.relative_to(upstream).as_posix()
            channel.find("description").text = channel_description
            for item in channel.findall("item"):
                link = item.findtext("link", "")
                row = index_by_path.get(urlsplit(link).path.removeprefix(prefix))
                if row:
                    item.find("title").text = row["title"]
                    item.find("description").text = row["content"][:500]
            tree.write(base / original_feed.relative_to(upstream), encoding="utf-8", xml_declaration=True)
    (output / "robots.txt").write_text(f"User-agent: *\nDisallow:\nSitemap: {SITE}/sitemap.xml\nSitemap: {SITE}/en/sitemap.xml\n")
    (output / ".nojekyll").touch()
    manifest_data = {"origin": ORIGIN, "site": SITE, "posts": manifest,
                     "translation_model": "GPT-6.1-sol",
                     "translation_method": "GPT-6.1-sol 子代理按三组分工直接逐段翻译，由主代理汇总并审核",
                     "term_policy": {"source": "translations/english_terms.json", "retained_english_terms": terms,
                                     "scope": "每篇文章的标题及正文；目录、参考文献、HTML属性、代码和公式不计入首次出现",
                                     "rule": "词表中的专名保留英文；首次紧跟中文括释，后续重复使用英文"},
                     "complete": all(p["status"] != "incomplete" for p in manifest)}
    write_json(ROOT / "translations.json", manifest_data)
    write_json(output / "translations.json", manifest_data)
    print(f"构建完成：{len(manifest)} 篇中英文文章，应用 {translated} 个翻译，尚缺 {missing} 个页面片段 -> {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["extract", "build"])
    parser.add_argument("--upstream", type=Path, default=ROOT / "upstream")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--source", type=Path, default=ROOT / "translations/source.json")
    parser.add_argument("--cache", type=Path, default=ROOT / "translations/cache.json")
    args = parser.parse_args()
    if args.action == "extract":
        extract(args.upstream, args.source)
    else:
        build(args.upstream, args.output, args.source, args.cache)
