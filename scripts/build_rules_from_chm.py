# -*- coding: utf-8 -*-
"""
@File     :   build_rules_from_chm.py
@Desc     :   CHM 离线预处理：读取 COC7th 七版基础规则 htm/html 页面，清洗为干货 Markdown 存入 data/rules
@Note     :   依赖 beautifulsoup4 + html2text（纯离线脚本，不走 LLM/存储链路）；
             按文件逐个转换，HTML 标题层级自动转为 MD（h1-># / h2->##），图片与无用超链接一律剥离；
             源目录默认七版基础规则，输出目录默认 data/rules，均可由命令行覆盖
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup
import html2text

# 源目录与输出目录（可在命令行覆盖）  # 状态：路径缺省
DEFAULT_SRC = Path(r"E:\coc\ai\rule\COC7thChm\七版基础规则")
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data" / "rules"

# 整块丢弃的标签：head 及其元信息、脚本、样式、表单等一律不进入正文  # 状态：清洗白名单
_DROP_TAGS = ("head", "script", "style", "noscript", "iframe", "form", "input")

# 归并连续空行（html2text 偶发多空行，压成单个空段）  # 状态：后处理
_BLANK_RE = re.compile(r"\n{3,}")


### 读取与清洗 ###


def read_text_auto(path: Path) -> str:
    """按编码自动探测读取：先 utf-8（含 BOM），失败再退 gb18030，兜底容错替换。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def clean_body(html: str) -> str:
    """剥掉无用的 head/脚本/样式/图片/装饰性 hr，并删除首个标题前的横幅内容，返回 <body> 内部 HTML。

    先整块丢 head/脚本/样式，再删图片与 hr；最后找到第一个 h1~h6，
    其之前的内容（居中横幅、导航条等页面装饰）一并剥离，正文从页面标题开始。
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    for img in soup.find_all("img"):
        img.decompose()
    for hr in soup.find_all("hr"):
        hr.decompose()
    body = soup.body or soup
    first_heading = body.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    if first_heading is not None:
        # 判祖先须用 id() 身份：bs4 Tag.__eq__ 按内容递归比较，删横幅会改内容导致 in 集合失效误删整树
        ancestor_ids = {id(anc) for anc in first_heading.parents}
        for prev in list(first_heading.find_all_previous()):
            if id(prev) not in ancestor_ids:
                prev.decompose()
    return str(body)


def build_converter() -> html2text.HTML2Text:
    """构造页面级转换器：图片/超链接剥离、中文不折行、列表用 '-' 表达。"""
    h = html2text.HTML2Text()
    h.ignore_images = True    # 图片一律剥离
    h.ignore_links = True     # 无用超链接剥成纯文本，仅保留锚文本
    h.body_width = 0          # 不按宽度折行，避免中文换行错乱
    h.unicode_snob = True
    h.ul_item_mark = "-"
    return h


def html_to_markdown(html: str) -> str:
    """html2text 转换 + 后处理：去行尾空白、归并空行、去首尾空行。"""
    md = build_converter().handle(html)
    lines = [ln.rstrip() for ln in md.splitlines()]
    text = _BLANK_RE.sub("\n\n", "\n".join(lines)).strip()
    return text + "\n"


def convert_file(src: Path, rel: Path, out_dir: Path) -> Path | None:
    """单个页面：读源 -> 清洗 -> 转 MD -> 按相对路径写入 out_dir，空结果返回 None。"""
    out = out_dir / rel.with_suffix(".md")
    raw = read_text_auto(src)
    md = html_to_markdown(clean_body(raw))
    if not md.strip():
        print(f"  [SKIP] {rel} 转换后为空，跳过")
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    return out


### 入口 ###


def main() -> int:
    parser = argparse.ArgumentParser(description="COC7th 七版基础规则 CHM 离线预处理")
    parser.add_argument(
        "--src", type=Path, default=DEFAULT_SRC, help="源 htm/html 目录（默认七版基础规则）"
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="输出 Markdown 目录（默认 data/rules）"
    )
    args = parser.parse_args()

    src_dir = args.src
    out_dir = args.out
    if not src_dir.is_dir():
        print(f"[FAIL] 源目录不存在: {src_dir}")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(
        p
        for p in src_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in (".htm", ".html")
    )
    if not files:
        print(f"[WARN] {src_dir} 下没有 .htm/.html 文件")
        return 0

    print(f"共发现 {len(files)} 个页面，开始转换 ...")
    ok_count = 0
    fail_list = []
    for f in files:
        rel = f.relative_to(src_dir)
        try:
            if convert_file(f, rel, out_dir):
                ok_count += 1
                print(f"  [OK ] {rel} -> data/rules/{rel.with_suffix('.md')}")
        except Exception as e:  # noqa: BLE001
            fail_list.append(rel)
            print(f"  [FAIL] {rel}: {type(e).__name__}: {e}")

    print(f"\n完成：成功 {ok_count}，失败 {len(fail_list)}，输出目录 {out_dir}")
    if fail_list:
        for rel in fail_list:
            print(f"  - {rel}")
    return 0 if not fail_list else 2


if __name__ == "__main__":
    sys.exit(main())
