#!/usr/bin/env python3
"""逐篇对照下载原文，核验中文站的结构、受保护内容和正文翻译覆盖。

本脚本不评价译文语义正确性；术语、论证和行文质量仍须人工审核。
"""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import html


CLASS_BODY = '//*[contains(concat(" ", normalize-space(@class), " "), " post-content ")]'
CLASS_TOC = '//*[contains(concat(" ", normalize-space(@class), " "), " toc ")]'
MATH = re.compile(r'(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$|(?<!\\)\$(?!\$)[^$]*?(?<!\\)\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)')
CHINESE = re.compile(r'[\u3400-\u9fff]')
WORDS = re.compile(r'[A-Za-z]+(?:[-\u2019\'][A-Za-z]+)*')
SITE_HOSTS = {'lilianweng.github.io', 'xingshuozhu1998.github.io'}


def visible_text(node):
    # 注释中的英语不展示给读者，代码/脚本也不作为待翻译正文。
    return ''.join(node.xpath('.//text()[not(ancestor::pre or ancestor::code or ancestor::script or ancestor::style or ancestor::svg)]'))


def normalized(text):
    return re.sub(r'\s+', ' ', text).strip()


def check_first_explanations(original, translated, terms):
    """按用户要求核对英文专名和首次中文简释，目录不占正文首次。"""
    def article_text(document):
        nodes = document.xpath('//h1[contains(concat(" ", normalize-space(@class), " "), " post-title ")]')
        nodes += document.xpath(CLASS_BODY)
        parts, references = [], False

        def walk(node):
            nonlocal references
            if not isinstance(node.tag, str) or node.tag in {'pre', 'code', 'script', 'style', 'svg', 'textarea', 'noscript'}:
                return
            if node.tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'} and 'post-title' not in (node.get('class') or '').split():
                references = bool(re.search(r'references|citation|papers-mentioned|blog-posts-mentioned|useful-resources|interesting-blogs', node.get('id', ''), re.I))
            if node.text and not references:
                parts.append(node.text)
            for child in node:
                walk(child)
                if child.tail and not references:
                    parts.append(child.tail)

        for node in nodes:
            walk(node)
            parts.append('\n')
        return MATH.sub('', ''.join(parts))

    pattern = re.compile(r'(?<![A-Za-z0-9_])(?:' + '|'.join(re.escape(term) for term in sorted(terms, key=len, reverse=True)) + r')(?![A-Za-z0-9_])', re.I)
    source_text, target_text = article_text(original), article_text(translated)
    source_terms = {match.group().casefold() for match in pattern.finditer(source_text)}
    explanations = {term.casefold(): meaning for term, meaning in terms.items()}
    seen, errors = set(), []
    for match in pattern.finditer(target_text):
        key = match.group().casefold()
        annotation = '（' + explanations[key] + '）'
        explained = target_text[match.end():].startswith(annotation)
        if key not in seen and not explained:
            errors.append('英文术语首次出现缺少约定的中文简释：' + match.group())
        elif key in seen and explained:
            errors.append('英文术语后续出现重复添加中文简释：' + match.group())
        seen.add(key)
    for term in sorted(source_terms - seen):
        errors.append('原文英文专名在译文中未保留：' + term)
    return errors


def math_fragments(node):
    return MATH.findall(visible_text(node))


def image_path(src, page_path, root, base_path):
    parsed = urlsplit(src)
    if (parsed.scheme or parsed.netloc) and parsed.netloc not in SITE_HOSTS:
        return src
    path = unquote(parsed.path)
    if base_path and path.startswith(base_path.rstrip('/') + '/'):
        path = path[len(base_path.rstrip('/')):]
    resolved = root / path.lstrip('/') if path.startswith('/') else page_path.parent / path
    return resolved.resolve().relative_to(root.resolve()).as_posix()


def check_post(source, target, upstream, dist, title, base_path, terms):
    slug = source.parent.name
    result = {'slug': slug, 'errors': [], 'warnings': []}
    if not target.exists():
        result['errors'].append('缺少中文文章')
        return result
    original = html.parse(str(source))
    translated = html.parse(str(target))
    source_bodies, target_bodies = original.xpath(CLASS_BODY), translated.xpath(CLASS_BODY)
    if len(source_bodies) != 1 or len(target_bodies) != 1:
        result['errors'].append('原文或译文正文容器数量不是1')
        return result
    original_body, translated_body = source_bodies[0], target_bodies[0]
    original_tags = [e.tag for e in original_body.iter() if isinstance(e.tag, str)]
    translated_tags = [e.tag for e in translated_body.iter() if isinstance(e.tag, str)]
    if original_tags != translated_tags:
        result['errors'].append('正文HTML元素顺序或数量发生变化')
    result['paragraphs'] = len(original_body.xpath('.//p'))
    if result['paragraphs'] != len(translated_body.xpath('.//p')):
        result['errors'].append('正文段落数量改变')

    original_math, translated_math = math_fragments(original_body), math_fragments(translated_body)
    result['math_fragments'] = len(original_math)
    if original_math != translated_math:
        result['errors'].append('公式内容或顺序发生变化')
        result['math_fragments_translated'] = len(translated_math)
    original_code = [e.text_content() for e in original_body.xpath('.//pre|.//code')]
    translated_code = [e.text_content() for e in translated_body.xpath('.//pre|.//code')]
    result['code_blocks_and_inline'] = len(original_code)
    if original_code != translated_code:
        result['errors'].append('代码内容或顺序发生变化')

    original_images = [image_path(e.get('src', ''), source, upstream, '') for e in original_body.xpath('.//img')]
    translated_images = [image_path(e.get('src', ''), target, dist, base_path) for e in translated_body.xpath('.//img')]
    result['images'] = len(original_images)
    if original_images != translated_images:
        result['errors'].append('正文图片地址或顺序发生变化')
    for path in translated_images:
        if not urlsplit(path).scheme and not (dist / path).is_file():
            result['errors'].append(f'缺少图片文件：{path}')

    original_ids = original_body.xpath('.//*[@id]/@id')
    translated_ids = translated_body.xpath('.//*[@id]/@id')
    if original_ids != translated_ids:
        result['errors'].append('正文标题/公式等节点的id改变')
    source_toc = original.xpath(CLASS_TOC + '//a/@href')
    target_toc = translated.xpath(CLASS_TOC + '//a/@href')
    result['toc_links'] = len(source_toc)
    if source_toc != target_toc:
        result['errors'].append('目录链接改变')
    all_ids = set(translated.xpath('//*[@id]/@id'))
    broken = [href for href in target_toc if href.startswith('#') and unquote(href[1:]) not in all_ids]
    if broken:
        result['errors'].append(f'目录存在无对应目标的锚点：{broken}')

    titles = translated.xpath('//h1[contains(concat(" ", normalize-space(@class), " "), " post-title ")]')
    if not titles or normalized(titles[0].text_content()) != title:
        result['errors'].append('文章标题未采用审核后的中文标题')
    page_terms = dict(terms)
    if 'Distributional Policy Gradient' in original_body.text_content():
        page_terms['DPG'] = '分布策略梯度，区别于确定性策略梯度'
    result['term_policy_errors'] = check_first_explanations(original, translated, page_terms)
    result['errors'].extend(result['term_policy_errors'])

    # 参考文献和引用格式保留英文；其余正文长段落若没有中文则列为漏译。
    in_references = False
    checked = untranslated = 0
    original_blocks = original_body.xpath('.//p|.//li|.//figcaption|.//h1|.//h2|.//h3|.//h4')
    translated_blocks = translated_body.xpath('.//p|.//li|.//figcaption|.//h1|.//h2|.//h3|.//h4')
    for src, dst in zip(original_blocks, translated_blocks):
        text = visible_text(src)
        if src.tag.startswith('h'):
            in_references = bool(re.search(r'\b(references|citation|cited as|papers mentioned|blog posts mentioned|useful resources|interesting blogs)\b', text, re.I))
        without_math = MATH.sub('', text)
        if in_references or len(WORDS.findall(without_math)) < 8:
            continue
        checked += 1
        translation = MATH.sub('', visible_text(dst))
        if not CHINESE.search(translation):
            untranslated += 1
            result['errors'].append('正文疑似整段漏译：' + normalized(without_math)[:180])
        elif normalized(without_math) == normalized(translation):
            untranslated += 1
            result['errors'].append('正文长段与原文相同：' + normalized(without_math)[:180])
        else:
            # 中英混合段落可能含正常英文术语/文献题名，因此只提示人工核查。
            for fragment in CHINESE.split(translation):
                if len(WORDS.findall(fragment)) >= 20:
                    result['warnings'].append('中文段落内仍有较长英文片段，请确认是术语/文献还是漏译：' + normalized(fragment)[:220])
    result['checked_prose_blocks'] = checked
    result['untranslated_prose_blocks'] = untranslated
    result['untranslated_block_rate'] = round(untranslated / checked, 5) if checked else 0
    # 标题为模型/算法名称时可以保留英语，较长的目录标题则应译成中文。
    for node in translated.xpath(CLASS_TOC + '//a'):
        text = MATH.sub('', node.text_content())
        if len(WORDS.findall(text)) >= 5 and not CHINESE.search(text):
            result['errors'].append('目录长标题未翻译：' + normalized(text))
    return result


def check_english_mirror(source, target, upstream, dist, base_path):
    """英文镜像应只改站内地址；展示文本与正文结构仍与原文一致。"""
    errors = []
    if not target.exists():
        return ['缺少英文镜像文章']
    original, mirror = html.parse(str(source)), html.parse(str(target))
    original_body = original.xpath(CLASS_BODY)[0]
    mirror_bodies = mirror.xpath(CLASS_BODY)
    if len(mirror_bodies) != 1:
        return ['英文镜像正文容器数量不是1']
    mirror_body = mirror_bodies[0]
    if [e.tag for e in original_body.iter() if isinstance(e.tag, str)] != [e.tag for e in mirror_body.iter() if isinstance(e.tag, str)]:
        errors.append('英文镜像正文HTML元素顺序或数量发生变化')
    if original_body.text_content() != mirror_body.text_content():
        errors.append('英文镜像正文展示文本发生变化')
    original_images = [image_path(e.get('src', ''), source, upstream, '') for e in original_body.xpath('.//img')]
    mirror_images = [image_path(e.get('src', ''), target, dist, base_path) for e in mirror_body.xpath('.//img')]
    if original_images != mirror_images:
        errors.append('英文镜像正文图片地址或顺序发生变化')
    for path in mirror_images:
        if not urlsplit(path).scheme and not (dist / path).is_file():
            errors.append(f'英文镜像缺少图片文件：{path}')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', type=Path, default=Path('upstream'))
    parser.add_argument('--dist', type=Path, default=Path('dist'))
    parser.add_argument('--titles', type=Path, default=Path('translations/titles.json'))
    parser.add_argument('--terms', type=Path, default=Path('translations/english_terms.json'))
    parser.add_argument('--report', type=Path, default=Path('translations/validation.json'))
    parser.add_argument('--base-path', default='', help='GitHub项目Pages站点路径前缀，例如 /lilianweng-cn')
    args = parser.parse_args()
    titles = json.loads(args.titles.read_text())
    terms = json.loads(args.terms.read_text())
    results = [check_post(args.upstream / 'posts' / slug / 'index.html', args.dist / 'posts' / slug / 'index.html', args.upstream, args.dist, title, args.base_path, terms) for slug, title in titles.items()]
    for row in results:
        slug = row['slug']
        row['english_mirror_errors'] = check_english_mirror(args.upstream / 'posts' / slug / 'index.html', args.dist / 'en' / 'posts' / slug / 'index.html', args.upstream, args.dist, args.base_path)
        row['errors'].extend(row['english_mirror_errors'])
    total_errors = sum(len(row['errors']) for row in results)
    report = {'post_count': len(results), 'passed_posts': sum(not row['errors'] for row in results), 'error_count': total_errors, 'warning_count': sum(len(row['warnings']) for row in results), 'totals': {key: sum(row.get(key, 0) for row in results) for key in ['paragraphs', 'math_fragments', 'code_blocks_and_inline', 'images', 'toc_links', 'checked_prose_blocks', 'untranslated_prose_blocks']}, 'posts': results, 'limitation': '结构与翻译覆盖检查通过不等于语义准确；仍须人工抽查译文。'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'posts'}, ensure_ascii=False, indent=2))
    for row in results:
        for error in row['errors'][:5]:
            print(f"{row['slug']}: {error}")
    raise SystemExit(bool(total_errors))


if __name__ == '__main__':
    main()
