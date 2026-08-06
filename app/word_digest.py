from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx.shared import Cm, Pt, RGBColor
import html


from .models import Article
from .source_registry import is_official_source, get_source_info, get_official_sources

CATEGORY_NAMES = {"politics": "政治新闻", "economy": "经济新闻", "international": "国际新闻"}
CATEGORY_ORDER = ["politics", "economy", "international"]
CATEGORY_NUMBERS = ["（一）", "（二）", "（三）"]
HYPERLINK_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"

def build_word_digest(articles: list[Article], output_dir: Path, generated_at: datetime | None = None, catch_up_urls: set[str] | None = None, importance_results: list | None = None) -> Path:
    if not articles:
        raise ValueError("No articles to generate Word digest")
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = generated_at or datetime.now()
    
    if catch_up_urls is None:
        catch_up_urls = set()
    fresh_count = len([a for a in articles if a.url not in catch_up_urls])
    catch_up_count = len([a for a in articles if a.url in catch_up_urls])
    official_articles = [a for a in articles if is_official_source(a.source_id)]
    media_articles = [a for a in articles if not is_official_source(a.source_id)]
    total = len(articles)
    official_count = len(official_articles)
    media_count = len(media_articles)
    
    doc = Document()
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.0)
    section.bottom_margin = Cm(2.0)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2.5)
    
    title = doc.add_heading("台湾新闻监测", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in title.runs:
        run.font.size = Pt(22)
    
    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = meta.add_run(f"生成时间：{generated_at.strftime('%Y年%m月%d日 %H:%M')}")
    run.font.size = Pt(11)
    run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    
    meta2 = doc.add_paragraph()
    meta2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run2 = meta2.add_run(f"新闻总数：{total}条")
    run2.font.size = Pt(11)
    run2.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    if catch_up_count > 0:
        run3a = meta2.add_run(f"\n正常新闻：{fresh_count}条")
        run3a.font.size = Pt(11)
        run3a.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
        run3b = meta2.add_run(f"\n补发新闻：{catch_up_count}条")
        run3b.font.size = Pt(11)
        run3b.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    run3 = meta2.add_run(f"\n官方信源：{official_count}条")
    run3.font.size = Pt(11)
    run3.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    run4 = meta2.add_run(f"\n媒体新闻：{media_count}条")
    run4.font.size = Pt(11)
    run4.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    
    heading_num = 0
    
    if official_articles:
        heading_num += 1
        doc.add_heading(f"{'一二三四五六七八九十'[heading_num-1]}、官方信源", level=1)
        official_by_source: dict[str, list[Article]] = {}
        for a in official_articles:
            official_by_source.setdefault(a.source_id, []).append(a)
        official_sources = get_official_sources()
        sub_idx = 0
        for order, sid, info in official_sources:
            if sid not in official_by_source:
                continue
            sub_idx += 1
            cat_arts = official_by_source[sid]
            cat_arts.sort(key=lambda x: (x.published_at.timestamp() if x.published_at else 0, x.position), reverse=True)
            doc.add_heading(f"{'一二三四五六七八九十'[sub_idx-1]}）{info['display_name']}", level=2)
            for idx, article in enumerate(cat_arts, 1):
                p = doc.add_paragraph()
                if importance_results:
                    _lev = next((r.level for a, r in importance_results if a is article), "")
                else:
                    _lev = ""
                _pfx = "【重大】" if _lev == "critical" else "【重点】" if _lev == "important" else ""
                if article.url in catch_up_urls:
                    display_title = f"{idx}. 【补发】{_pfx}{article.title}"
                else:
                    display_title = f"{idx}. {_pfx}{article.title}"
                run = p.add_run(display_title)
                run.bold = True
                run.font.size = Pt(12)

                if article.summary:
                    p = doc.add_paragraph()
                    run = p.add_run(f"梗概：{article.summary}")
                    run.font.size = Pt(10)
                    run.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
                    run.italic = True
                
                p = doc.add_paragraph()
                run = p.add_run(f"来源：{article.source_name}")
                run.font.size = Pt(10)
                run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
                
                p = doc.add_paragraph()
                run = p.add_run(f"类型：{info['document_type']}")
                run.font.size = Pt(10)
                run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
                
                if article.published_at:
                    p = doc.add_paragraph()
                    run = p.add_run(f"发布时间：{article.published_at.strftime('%Y-%m-%d %H:%M')}")
                    run.font.size = Pt(10)
                    run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
                
                p = doc.add_paragraph()
                if article.url in catch_up_urls:
                    p = doc.add_paragraph()
                    run = p.add_run("状态：补发")
                    run.font.size = Pt(10)
                    run.font.color.rgb = RGBColor(0xCC, 0x66, 0x00)
                p = doc.add_paragraph()
                _add_hyperlink(p, article.url, article.url)
                doc.add_paragraph()
    
    if media_articles:
        heading_num += 1
        doc.add_heading(f"{'一二三四五六七八九十'[heading_num-1]}、媒体新闻", level=1)
        grouped: dict[str, list[Article]] = {}
        for cat in CATEGORY_ORDER:
            grouped[cat] = []
        for article in media_articles:
            if article.category in grouped:
                grouped[article.category].append(article)
        for cat_arts in grouped.values():
            cat_arts.sort(key=lambda x: (x.published_at.timestamp() if x.published_at else 0, x.position), reverse=True)
        
        cat_index = 0
        for cat in CATEGORY_ORDER:
            cat_articles = grouped.get(cat, [])
            if not cat_articles:
                continue
            cat_name = CATEGORY_NAMES.get(cat, cat)
            num = CATEGORY_NUMBERS[cat_index]
            cat_index += 1
            doc.add_heading(f"{num}{cat_name}", level=1)
            for idx, article in enumerate(cat_articles, 1):
                p = doc.add_paragraph()
                if importance_results:
                    _lev = next((r.level for a, r in importance_results if a is article), "")
                else:
                    _lev = ""
                _pfx = "【重大】" if _lev == "critical" else "【重点】" if _lev == "important" else ""
                if article.url in catch_up_urls:
                    display_title = f"{idx}. 【补发】{_pfx}{article.title}"
                else:
                    display_title = f"{idx}. {_pfx}{article.title}"
                run = p.add_run(display_title)
                run.bold = True
                run.font.size = Pt(12)

                if article.summary:
                    p = doc.add_paragraph()
                    run = p.add_run(f"梗概：{article.summary}")
                    run.font.size = Pt(10)
                    run.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
                    run.italic = True
                
                p = doc.add_paragraph()
                run = p.add_run(f"来源：{article.source_name}")
                run.font.size = Pt(10)
                run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
                
                if article.published_at:
                    p = doc.add_paragraph()
                    run = p.add_run(f"发布时间：{article.published_at.strftime('%Y-%m-%d %H:%M')}")
                    run.font.size = Pt(10)
                    run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
                
                p = doc.add_paragraph()
                if article.url in catch_up_urls:
                    p = doc.add_paragraph()
                    run = p.add_run("状态：补发")
                    run.font.size = Pt(10)
                    run.font.color.rgb = RGBColor(0xCC, 0x66, 0x00)
                p = doc.add_paragraph()
                _add_hyperlink(p, article.url, article.url)
                doc.add_paragraph()
    
    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("本简报由自动新闻监测程序生成，具体内容以原媒体报道为准。")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)
    run.italic = True
    
    filename = f"台湾新闻监测_{generated_at.strftime('%Y-%m-%d_%H%M')}.docx"
    output_path = output_dir / filename
    doc.save(str(output_path))
    return output_path

def _add_hyperlink(paragraph, url: str, text: str) -> None:
    part = paragraph.part
    r_id = part.relate_to(url, HYPERLINK_REL_TYPE, is_external=True)
    safe_text = html.escape(text)
    hyperlink_xml = (
        f'<w:hyperlink {nsdecls("w")} r:id="{r_id}" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<w:r><w:rPr><w:color w:val=\"0563C1\"/><w:u w:val=\"single\"/><w:sz w:val=\"20\"/></w:rPr>"
        f"<w:t xml:space=\"preserve\">{safe_text}</w:t></w:r></w:hyperlink>"
    )
    hyperlink = parse_xml(hyperlink_xml)
    paragraph._p.append(hyperlink)
