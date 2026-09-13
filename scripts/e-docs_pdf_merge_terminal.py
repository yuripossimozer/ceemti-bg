import io
import logging
import os
import sys
import re
import uuid
import tempfile
import shutil
import atexit
import unicodedata

logging.basicConfig(stream=sys.stdout, level=logging.ERROR, format='%(levelname)s: %(message)s')

# ==========================================
# VERIFICAÇÃO INICIAL DE DEPENDÊNCIAS
# ==========================================
missing_packages = []

try:
    import fitz
except ImportError:
    missing_packages.append("PyMuPDF")

try:
    import pypdf
except ImportError:
    missing_packages.append("pypdf")

try:
    import rich
except ImportError:
    missing_packages.append("rich")

if missing_packages:
    print("\n[ERRO FATAL] O script não pode ser iniciado por falta de dependências.")
    print("Os seguintes pacotes não foram encontrados:")
    for pkg in missing_packages:
        print(f"  - {pkg}")
    print(f"\nPor favor, instale as dependências executando o comando abaixo:")
    print(f"pip install {' '.join(missing_packages)}\n")
    sys.exit(1)

# Se o código chegou até aqui, todas as bibliotecas estão disponíveis.
# Mantemos as flags como True para não quebrar a lógica interna do restante do script.
PYMUPDF_AVAILABLE = True
PYPDF_AVAILABLE = True

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, ArrayObject, DictionaryObject, StreamObject

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt
from rich import print as rprint

CONSOLE = Console()

EDOCS_SHIFT_LEFT_PTS = 1
EDOCS_STAMP_OMIT_EDOCS_WORD = False

# ==========================================
# ROTINA DE LIMPEZA DE ARQUIVOS TEMPORÁRIOS
# ==========================================

def cleanup_temp_files():
    """Remove a pasta de arquivos temporários e todos os PDFs residuais ao encerrar o programa."""
    temp_dir = os.path.join(tempfile.gettempdir(), 'pdf_editor_temp')
    if os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir)
            logging.debug("Arquivos temporários apagados com sucesso.")
        except Exception as e:
            logging.error(f"Erro ao apagar a pasta de arquivos temporários: {e}")

# Garante que a rotina seja executada quando o script for finalizado
atexit.register(cleanup_temp_files)

# ==========================================
# FUNÇÕES CORE DE PDF (SINCRONIZADAS COM A VERSÃO GRÁFICA)
# ==========================================

def clean_pdf_signatures(input_path: str) -> tuple[str, dict]:
    """Remove assinaturas do PDF e achata o visual, retornando o caminho e os status de limpeza."""
    stats = {
        "acroform_removed": False,
        "perms_removed": False,
        "sigflags_removed": False,
        "widgets_flattened": 0,
        "error": None
    }
    try:
        original_name = os.path.basename(input_path)
        temp_dir = os.path.join(tempfile.gettempdir(), 'pdf_editor_temp')
        os.makedirs(temp_dir, exist_ok=True)
        
        unique_id = uuid.uuid4().hex[:8]
        output_path = os.path.join(temp_dir, f"{unique_id}_{original_name}")
        
        reader = PdfReader(input_path)
        writer = PdfWriter()
        
        for page in reader.pages:
            writer.add_page(page)
            
        # 1. Destrói o registro global de formulários e segurança criptográfica
        for key in ["/AcroForm", "/Perms", "/SigFlags"]:
            if key in writer.root_object:
                del writer.root_object[key]
                if key == "/AcroForm": stats["acroform_removed"] = True
                if key == "/Perms": stats["perms_removed"] = True
                if key == "/SigFlags": stats["sigflags_removed"] = True
                
        # 2. Varrer anotações, achatar o visual da assinatura (Flattening) e remover o widget
        for page in writer.pages:
            if "/Annots" in page:
                annots = page["/Annots"].get_object()
                if isinstance(annots, list) or isinstance(annots, ArrayObject):
                    new_annots = ArrayObject()
                    
                    for annot_ref in annots:
                        try:
                            annot = annot_ref.get_object()
                            if annot.get("/Subtype") == "/Widget" and annot.get("/FT") == "/Sig":
                                stats["widgets_flattened"] += 1
                                ap = annot.get("/AP")
                                if ap:
                                    ap_obj = ap.get_object()
                                    ap_n_ref = ap_obj.get("/N")
                                    ap_n_obj = ap_n_ref.get_object() if ap_n_ref else None
                                    
                                    rect = annot.get("/Rect")
                                    
                                    if ap_n_obj and rect:
                                        # Evita o erro de salvamento forçando um ponteiro indireto se não existir
                                        if getattr(ap_n_ref, "indirect_reference", None) is None:
                                            ap_n_ref = writer._add_object(ap_n_obj)
                                        
                                        # Calculando Escala e Translação exatas (BBox -> Rect)
                                        rx0, ry0, rx1, ry1 = float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])
                                        bbox = ap_n_obj.get("/BBox")
                                        if bbox:
                                            bx0, by0, bx1, by1 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
                                        else:
                                            bx0, by0, bx1, by1 = 0.0, 0.0, rx1 - rx0, ry1 - ry0
                                            
                                        bw = bx1 - bx0
                                        bh = by1 - by0
                                        rw = rx1 - rx0
                                        rh = ry1 - ry0
                                        
                                        sx = rw / bw if bw != 0 else 1.0
                                        sy = rh / bh if bh != 0 else 1.0
                                        tx = rx0 - (bx0 * sx)
                                        ty = ry0 - (by0 * sy)
                                        
                                        # Registra a imagem nos recursos da página
                                        resources = page.get("/Resources", DictionaryObject()).get_object()
                                        if "/Resources" not in page:
                                            page[NameObject("/Resources")] = resources
                                            
                                        xobjects = resources.get("/XObject", DictionaryObject()).get_object()
                                        if "/XObject" not in resources:
                                            resources[NameObject("/XObject")] = xobjects
                                            
                                        xobj_name = NameObject(f"/FmSig_{uuid.uuid4().hex[:6]}")
                                        xobjects[xobj_name] = ap_n_ref
                                        
                                        # Cria o bloco de isolamento final com cálculo de matriz completo
                                        content_str = f"\nQ\nq {sx} 0 0 {sy} {tx} {ty} cm {xobj_name} Do Q\n".encode("utf-8")
                                        
                                        contents = page.get("/Contents")
                                        if contents:
                                            contents_obj = contents.get_object()
                                            
                                            # Manipulação limpa que encapsula o stream original sem quebrar matrizes
                                            if isinstance(contents_obj, ArrayObject):
                                                first_stream = contents_obj[0].get_object()
                                                last_stream = contents_obj[-1].get_object()
                                                
                                                if hasattr(first_stream, "get_data"):
                                                    first_stream.set_data(b"q\n" + first_stream.get_data())
                                                else:
                                                    first_stream._data = b"q\n" + first_stream._data
                                                    
                                                if hasattr(last_stream, "get_data"):
                                                    last_stream.set_data(last_stream.get_data() + content_str)
                                                else:
                                                    last_stream._data += content_str
                                            else:
                                                target_stream = contents_obj
                                                if hasattr(target_stream, "get_data"):
                                                    target_stream.set_data(b"q\n" + target_stream.get_data() + content_str)
                                                else:
                                                    target_stream._data = b"q\n" + target_stream._data + content_str
                                        else:
                                            # Trata páginas puramente em branco
                                            new_stream = StreamObject()
                                            content_only = f"\nq {sx} 0 0 {sy} {tx} {ty} cm {xobj_name} Do Q\n".encode("utf-8")
                                            if hasattr(new_stream, "set_data"):
                                                new_stream.set_data(content_only)
                                            else:
                                                new_stream._data = content_only
                                            page[NameObject("/Contents")] = writer._add_object(new_stream)
                                            
                                # O Widget de Assinatura não é mais incluído. Restará só a imagem achatada nativamente.
                            else:
                                new_annots.append(annot_ref)
                        except Exception:
                            new_annots.append(annot_ref)
                            
                    if len(new_annots) > 0:
                        page[NameObject("/Annots")] = new_annots
                    else:
                        if "/Annots" in page:
                            del page["/Annots"]
                        
        with open(output_path, "wb") as f:
            writer.write(f)
            
        return output_path, stats
    except Exception as e:
        logging.error(f"Erro na limpeza de assinatura do arquivo {input_path}: {e}")
        stats["error"] = str(e)
        return input_path, stats

def get_display_name(filepath: str) -> str:
    """Remove o prefixo UUID (se existir) para exibir o nome original na interface."""
    name = os.path.basename(filepath)
    if re.match(r'^[0-9a-f]{8}_', name):
        return name[9:]
    return name

def _strip_edocs_token_from_text(text: str) -> str:
    """Remove apenas o token E-DOCS / E DOCS / EDOCS da string (uma linha de carimbo)."""
    if not text:
        return ""
    t = re.sub(r"\bE\s*[- ]?\s*DOCS\b", " ", text, flags=re.IGNORECASE)
    t = re.sub(r"\bEDOCS\b", " ", t, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip()

def _looks_like_url_or_domain_token(text: str) -> bool:
    """True se o fragmento de texto parece URL / domínio."""
    if not text:
        return False
    t = text.lower().strip()
    if "://" in t or "http" in t or "www." in t or "mailto:" in t:
        return True
    if "e-docs.es" in t or ("e-docs." in t and ".gov" in t):
        return True
    if ".gov.br" in t or ".es.gov" in t:
        return True
    if t.startswith("https") or t.startswith("http"):
        return True
    return False

def _is_standalone_edocs_stamp_token(word_text: str) -> bool:
    """E-DOCS do carimbo oficial (não 'e-docs' dentro de URL/domínio)."""
    if _looks_like_url_or_domain_token(word_text):
        return False
    if "." in word_text or "/" in word_text:
        return False
    w = _normalize_text_for_edocs(word_text).upper().strip()
    compact = w.replace("-", "").replace(" ", "")
    if w in {"E-DOCS", "E DOCS", "EDOCS", "DOCS"}:
        return True
    if re.fullmatch(r"E\s*[- ]?\s*DOCS", w, re.IGNORECASE):
        return True
    return compact == "EDOCS"

def _int_color_to_rgb01(color_int: int) -> tuple[float, float, float]:
    try:
        r = ((color_int >> 16) & 0xFF) / 255.0
        g = ((color_int >> 8) & 0xFF) / 255.0
        b = (color_int & 0xFF) / 255.0
        return (r, g, b)
    except Exception:
        return (0.0, 0.0, 0.0)

def shift_edocs_stamp_left_in_page(page, dx_pts: float = EDOCS_SHIFT_LEFT_PTS) -> bool:
    try:
        page_rect = page.rect
        detect_x0 = page_rect.x1 - page_rect.width * 0.22
        margin_x0 = detect_x0

        all_anchor_rects = []
        for term in ("E-DOCS", "E DOCS", "EDOCS"):
            for r in page.search_for(term) or []:
                if r.x0 >= margin_x0:
                    all_anchor_rects.append(r)

        right_strip_x0 = detect_x0
        h_band = 24.0
        words = page.get_text("words") or []
        if not words:
            return False

        code_pattern = re.compile(r"^\d{2,4}-[A-Z0-9]{4,}$", re.IGNORECASE)
        date_pattern = re.compile(r"^\d{2}/\d{2}/\d{4}$")
        time_pattern = re.compile(r"^\d{2}:\d{2}$")
        page_pattern = re.compile(r"^\d+/\d+$")

        def _stamp_score(text: str) -> int:
            w = _normalize_text_for_edocs(text).upper().strip()
            if not w:
                return 0
            score = 0
            if _is_standalone_edocs_stamp_token(text):
                score += 3
            if w in {"CÓPIA", "COPIA", "SIMPLES", "PÁGINA", "PAGINA", "DOCUMENTO", "ORIGINAL"}:
                score += 1
            if code_pattern.match(w):
                score += 2
            if date_pattern.match(w) or time_pattern.match(w) or page_pattern.match(w):
                score += 1
            return score

        def _is_strong_stamp_anchor_token(text: str) -> bool:
            w = _normalize_text_for_edocs(text).upper().strip()
            if not w:
                return False
            if _looks_like_url_or_domain_token(text):
                return False
            if _is_standalone_edocs_stamp_token(text):
                return True
            if code_pattern.match(w):
                return True
            if w in {"CÓPIA", "COPIA", "SIMPLES", "DOCUMENTO", "ORIGINAL"}:
                return True
            return False

        def _is_stamp_token(word_text: str) -> bool:
            if _looks_like_url_or_domain_token(word_text):
                return False
            w = _normalize_text_for_edocs(word_text).upper().strip()
            if not w:
                return False
            if w == "E":
                return False
            if _is_standalone_edocs_stamp_token(word_text):
                return True
            if w in {"CÓPIA", "COPIA", "SIMPLES", "PÁGINA", "PAGINA", "DOCUMENTO", "ORIGINAL"}:
                return True
            if code_pattern.match(w) or date_pattern.match(w) or time_pattern.match(w) or page_pattern.match(w):
                return True
            if w.isdigit() and len(w) <= 4:
                return True
            if w in {"/", "-", ":"}:
                return True
            return False

        for word in words:
            if not isinstance(word, (tuple, list)) or len(word) < 5:
                continue
            x0, y0, x1, y1, text = word[0], word[1], word[2], word[3], str(word[4] or "")
            if x0 < detect_x0:
                continue
            if _is_strong_stamp_anchor_token(text):
                all_anchor_rects.append(fitz.Rect(x0, y0, x1, y1))

        if not all_anchor_rects:
            return False

        def _union_cluster(rects):
            u = rects[0]
            for rr in rects[1:]:
                u |= rr
            return u

        def _collect_stamp_rect_for_anchor(anchor_rect):
            anchor_cx = (anchor_rect.x0 + anchor_rect.x1) / 2
            anchor_cy = (anchor_rect.y0 + anchor_rect.y1) / 2
            band_words = []
            for word in words:
                if not isinstance(word, (tuple, list)) or len(word) < 5:
                    continue
                x0, y0, x1, y1, text = word[0], word[1], word[2], word[3], str(word[4] or "")
                if x0 < right_strip_x0:
                    continue
                cx = (x0 + x1) / 2
                if abs(cx - anchor_cx) > h_band:
                    continue
                if not _is_stamp_token(text):
                    continue
                band_words.append((fitz.Rect(x0, y0, x1, y1), y0))

            if not band_words:
                return anchor_rect

            band_words.sort(key=lambda t: t[1])
            clusters = []
            cur = [band_words[0][0]]
            for i in range(1, len(band_words)):
                r_prev = cur[-1]
                r_next = band_words[i][0]
                gap = r_next.y0 - r_prev.y1
                if gap > 36:
                    clusters.append(cur)
                    cur = [r_next]
                else:
                    cur.append(r_next)
            clusters.append(cur)

            stamp_cluster = None
            for cl in clusters:
                union = _union_cluster(cl)
                overlaps_anchor = (union.y1 >= anchor_rect.y0) and (union.y0 <= anchor_rect.y1)
                if overlaps_anchor:
                    stamp_cluster = cl
                    break
            if stamp_cluster is None and clusters:
                stamp_cluster = min(
                    clusters,
                    key=lambda cl: abs(anchor_cy - ((_union_cluster(cl).y0 + _union_cluster(cl).y1) / 2)),
                )
            if not stamp_cluster:
                return anchor_rect
            return _union_cluster(stamp_cluster)

        pad_x = 6.0
        pad_y = 6.0
        margin_clip = fitz.Rect(margin_x0, page_rect.y0, page_rect.x1, page_rect.y1)

        stamp_rects = []
        for anchor in all_anchor_rects:
            rect = _collect_stamp_rect_for_anchor(anchor)
            rect = fitz.Rect(
                max(page_rect.x0, rect.x0 - pad_x),
                max(page_rect.y0, rect.y0 - pad_y),
                min(page_rect.x1, rect.x1 + pad_x),
                min(page_rect.y1, rect.y1 + pad_y),
            ) & margin_clip
            if rect.is_empty:
                continue
            stamp_rects.append(rect)

        if not stamp_rects:
            return False

        stamp_rects.sort(key=lambda r: (r.y0, r.x0))
        merged = []
        for rect in stamp_rects:
            if not merged:
                merged.append(rect)
                continue
            prev = merged[-1]
            same_col = abs(((prev.x0 + prev.x1) / 2) - ((rect.x0 + rect.x1) / 2)) <= 20
            close_y = (rect.y0 - prev.y1) <= 24
            overlap = (rect & prev).get_area() > 0
            if overlap or (same_col and close_y):
                merged[-1] = prev | rect
            else:
                merged.append(rect)

        payloads = []
        for stamp_rect in merged:
            is_vertical = stamp_rect.height > (stamp_rect.width * 1.4)
            base_text = page.get_textbox(stamp_rect).strip()
            if "://" in base_text or base_text.lower().startswith("http"):
                continue
            score_hits = 0
            strong_hits = 0
            for token in re.split(r"\s+", base_text):
                if _stamp_score(token) > 0:
                    score_hits += 1
                if _is_strong_stamp_anchor_token(token):
                    strong_hits += 1
            if score_hits < 2 or strong_hits < 1:
                continue
            if is_vertical:
                base_text = _strip_edocs_token_from_text(base_text)
            elif EDOCS_STAMP_OMIT_EDOCS_WORD:
                base_text = _strip_edocs_token_from_text(base_text)
            else:
                if not base_text:
                    base_text = "E-DOCS"
                elif "E-DOCS" not in base_text.upper().replace(" ", ""):
                    base_text = f"{base_text} - E-DOCS"
            new_rect = stamp_rect
            if new_rect.is_empty:
                new_rect = stamp_rect
            payloads.append((stamp_rect, new_rect, base_text, is_vertical))

        if not payloads:
            return False

        for stamp_rect, _, _, _ in payloads:
            page.add_redact_annot(stamp_rect, fill=None)
        page.apply_redactions(images=0, graphics=0)

        payloads.sort(key=lambda p: p[0].x0, reverse=True)
        adjusted = []
        stack_gap = 4.0
        edge_pad = 2.0
        column_x0 = None
        line_y0 = None
        max_w = 0.0
        
        for stamp_rect, _, _, _ in payloads:
            probe = (stamp_rect + (-dx_pts, 0, -dx_pts, 0)) & page_rect
            if probe.is_empty:
                probe = stamp_rect
            if column_x0 is None or probe.x0 > column_x0:
                column_x0 = probe.x0
            if line_y0 is None or probe.y0 < line_y0:
                line_y0 = probe.y0
            if probe.width > max_w:
                max_w = probe.width
                
        if column_x0 is None:
            return False
        if line_y0 is None:
            line_y0 = page_rect.y0 + edge_pad
        if max_w <= 0:
            max_w = 14.0

        step_x = max_w + stack_gap

        for idx, (stamp_rect, _, base_text, is_vertical) in enumerate(payloads):
            new_rect = (stamp_rect + (-dx_pts, 0, -dx_pts, 0)) & page_rect
            if new_rect.is_empty:
                new_rect = stamp_rect
            w = new_rect.width
            h = new_rect.height
            target_x0 = column_x0 - (idx * step_x)
            target_y0 = line_y0
            new_rect = fitz.Rect(target_x0, target_y0, target_x0 + w, target_y0 + h)

            if new_rect.x0 < page_rect.x0 + edge_pad:
                delta = (page_rect.x0 + edge_pad) - new_rect.x0
                new_rect = new_rect + (delta, 0, delta, 0)
            if new_rect.x1 > page_rect.x1 - edge_pad:
                delta = new_rect.x1 - (page_rect.x1 - edge_pad)
                new_rect = new_rect + (-delta, 0, -delta, 0)
            if new_rect.y1 > page_rect.y1 - edge_pad:
                overflow = new_rect.y1 - (page_rect.y1 - edge_pad)
                new_rect = new_rect + (0, -overflow, 0, -overflow)
            if new_rect.y0 < page_rect.y0 + edge_pad:
                underflow = (page_rect.y0 + edge_pad) - new_rect.y0
                new_rect = new_rect + (0, underflow, 0, underflow)

            adjusted.append((stamp_rect, new_rect, base_text, is_vertical))

        shape = page.new_shape()
        fontsize = 7.5
        for _, new_rect, base_text, is_vertical in adjusted:
            if is_vertical:
                shape.insert_text(
                    fitz.Point(new_rect.x0, new_rect.y1),
                    base_text,
                    fontsize=fontsize,
                    fontname="helv",
                    color=(0, 0, 0),
                    rotate=90
                )
            else:
                shape.insert_textbox(
                    new_rect,
                    base_text,
                    fontsize=fontsize,
                    fontname="helv",
                    color=(0, 0, 0),
                    align=fitz.TEXT_ALIGN_LEFT
                )
        shape.commit()

        return True
    except Exception as e:
        logging.error(f"Erro na vetorização: {e}")
        return False

def rasterize_edocs_marker_in_page(page, dx_pts: float = EDOCS_SHIFT_LEFT_PTS, dpi: int = 300) -> bool:
    """Rasteriza apenas os blocos de texto da marcação E-DOCS e mantém o restante vetorial."""
    try:
        page_rect = page.rect
        candidate_rects = []
        right_margin_limit = page_rect.x0 + (page_rect.width * 0.62)

        code_pattern = re.compile(r"^\d{2,4}-[A-Z0-9]{4,}$", re.IGNORECASE)
        date_pattern = re.compile(r"^\d{2}/\d{2}/\d{4}$")
        time_pattern = re.compile(r"^\d{2}:\d{2}$")
        page_pattern = re.compile(r"^\d+/\d+$")

        def _is_edocs_word(word_text: str) -> bool:
            w = _normalize_text_for_edocs(word_text).upper().strip()
            if not w:
                return False
            keyword_hits = {
                "E-DOCS", "E", "DOCS", "EDOCS", "COPIA", "CÓPIA",
                "SIMPLES", "DOCUMENTO", "ORIGINAL", "PAGINA", "PÁGINA"
            }
            if w in keyword_hits:
                return True
            if code_pattern.match(w) or date_pattern.match(w) or time_pattern.match(w) or page_pattern.match(w):
                return True
            return False

        blocks = page.get_text("blocks") or []
        for block in blocks:
            if not isinstance(block, (tuple, list)) or len(block) < 5:
                continue
            block_text = str(block[4] or "").strip()
            if not block_text:
                continue
            if detect_edocs_marking(block_text):
                block_rect = fitz.Rect(block[0], block[1], block[2], block[3]) & page_rect
                if not block_rect.is_empty:
                    candidate_rects.append(block_rect)

        if not candidate_rects:
            words = page.get_text("words") or []
            for word in words:
                if not isinstance(word, (tuple, list)) or len(word) < 5:
                    continue
                x0, y0, x1, y1, text = word[0], word[1], word[2], word[3], str(word[4] or "")
                if x0 < right_margin_limit:
                    continue
                if _is_edocs_word(text):
                    rect = fitz.Rect(x0, y0, x1, y1) & page_rect
                    if not rect.is_empty:
                        candidate_rects.append(rect)

        if not candidate_rects:
            found_rects = page.search_for("E-DOCS")
            if not found_rects:
                found_rects = page.search_for("E DOCS")
            if not found_rects:
                found_rects = page.search_for("EDOCS")
            candidate_rects.extend(found_rects)

        if not candidate_rects:
            return False

        merged_rects = []
        pad_x = 8.0
        pad_y = 4.0
        for rect in candidate_rects:
            expanded = fitz.Rect(
                max(0, rect.x0 - pad_x),
                max(0, rect.y0 - pad_y),
                min(page_rect.x1, rect.x1 + pad_x),
                min(page_rect.y1, rect.y1 + pad_y),
            )
            if expanded.is_empty:
                continue

            merged = False
            for i, current in enumerate(merged_rects):
                if current.intersects(expanded) or abs(current.y0 - expanded.y0) < 12 or abs(current.x0 - expanded.x0) < 12:
                    merged_rects[i] = current | expanded
                    merged = True
                    break
            if not merged:
                merged_rects.append(expanded)

        if not merged_rects:
            return False

        captured_regions = []
        for marker_rect in merged_rects:
            marker_pix = page.get_pixmap(clip=marker_rect, dpi=dpi, alpha=True)
            marker_png = marker_pix.tobytes("png")
            captured_regions.append((marker_rect, marker_png))
            page.add_redact_annot(marker_rect, fill=None)

        page.apply_redactions(images=0, graphics=0)

        for marker_rect, marker_png in captured_regions:
            shifted_rect = marker_rect + (-dx_pts, 0, -dx_pts, 0)
            shifted_rect = shifted_rect & page_rect
            if shifted_rect.is_empty:
                shifted_rect = marker_rect
            page.insert_image(shifted_rect, stream=marker_png, keep_proportion=False, overlay=True)

        return True
    except Exception as e:
        logging.error(f"Erro ao rasterizar marcação E-DOCS: {e}")
        return False

def build_single_page_image_pdf_bytes(page, dpi: int = 300) -> bytes:
    """Converte uma página inteira em um PDF de imagem (sem camada de texto)."""
    pix = page.get_pixmap(dpi=dpi, alpha=False, annots=True)
    png_bytes = pix.tobytes("png")
    out_doc = fitz.open()
    out_page = out_doc.new_page(width=page.rect.width, height=page.rect.height)
    out_page.insert_image(out_page.rect, stream=png_bytes)
    out_buf = io.BytesIO()
    out_doc.save(out_buf, garbage=4, deflate=True, clean=True)
    out_doc.close()
    out_buf.seek(0)
    return out_buf.getvalue()

def detect_signed_pages_in_pdf(pdf_path: str) -> set[int]:
    """Detecta páginas com assinatura digital em campos /Sig (ICP-Brasil, Adobe e afins)."""
    signed_pages = set()

    if PYMUPDF_AVAILABLE:
        try:
            doc = fitz.open(pdf_path)
            sig_widget_type = getattr(fitz, "PDF_WIDGET_TYPE_SIGNATURE", None)
            for pno in range(doc.page_count):
                page = doc.load_page(pno)
                widgets = page.widgets()
                if not widgets:
                    continue
                for w in widgets:
                    ftype = getattr(w, "field_type", None)
                    ftype_str = str(getattr(w, "field_type_string", "") or "").upper()
                    if (sig_widget_type is not None and ftype == sig_widget_type) or ftype_str == "SIGNATURE":
                        signed_pages.add(pno)
                        break
            doc.close()
        except Exception as e:
            logging.debug(f"Falha na detecção de assinatura por widgets em {pdf_path}: {e}")

    if PYPDF_AVAILABLE:
        try:
            reader = PdfReader(pdf_path)
            page_ref_to_index = {}
            for idx, pg in enumerate(reader.pages):
                pref = getattr(pg, "indirect_reference", None)
                if pref is not None:
                    page_ref_to_index[(pref.idnum, pref.generation)] = idx

            unresolved_sig_found = False

            def _page_index_from_ref(pref):
                if pref is None:
                    return None
                try:
                    return page_ref_to_index.get((pref.idnum, pref.generation))
                except Exception:
                    return None

            def _iter_fields(field_refs):
                for fref in field_refs or []:
                    try:
                        fobj = fref.get_object()
                    except Exception:
                        continue
                    if not isinstance(fobj, dict):
                        continue
                    yield fobj
                    for kid in _iter_fields(fobj.get("/Kids", [])):
                        yield kid

            root = reader.trailer.get("/Root", {})
            acro = root.get("/AcroForm", {})
            fields = acro.get("/Fields", [])

            for field in _iter_fields(fields):
                ft = str(field.get("/FT", "") or "")
                v = field.get("/V")
                sig_dict = None
                if v is not None:
                    try:
                        sig_dict = v.get_object()
                    except Exception:
                        sig_dict = None

                is_sig = (ft == "/Sig")
                if sig_dict and isinstance(sig_dict, dict):
                    if sig_dict.get("/Contents") is not None:
                        is_sig = True
                    subf = str(sig_dict.get("/SubFilter", "") or "").upper()
                    if "PKCS7" in subf or "CADES" in subf:
                        is_sig = True

                if not is_sig:
                    continue

                page_idx = _page_index_from_ref(field.get("/P"))
                if page_idx is not None:
                    signed_pages.add(page_idx)
                    continue

                found_from_kid = False
                for kid_ref in field.get("/Kids", []):
                    try:
                        kid_obj = kid_ref.get_object()
                    except Exception:
                        continue
                    kid_page_idx = _page_index_from_ref(kid_obj.get("/P"))
                    if kid_page_idx is not None:
                        signed_pages.add(kid_page_idx)
                        found_from_kid = True
                if not found_from_kid:
                    unresolved_sig_found = True

            if unresolved_sig_found and len(reader.pages) > 0 and not signed_pages:
                signed_pages = set(range(len(reader.pages)))
        except Exception as e:
            logging.debug(f"Falha na detecção estrutural de assinatura em {pdf_path}: {e}")

    return signed_pages

def _normalize_text_for_edocs(text: str) -> str:
    """Normaliza texto para detecção E-DOCS: hífen unicode, espaços e quebras de linha."""
    if not text:
        return ""
    t = text.replace("\u2010", "-").replace("\u2011", "-").replace("\u2212", "-")
    t = re.sub(r"\s+", " ", t)
    return t.strip()

def detect_edocs_marking(page_text: str) -> bool:
    """Detecta se uma página contém a marcação E-DOCS de forma robusta."""
    if not page_text:
        return False

    normalized = _normalize_text_for_edocs(page_text)
    upper = normalized.upper()
    ascii_upper = "".join(
        c for c in unicodedata.normalize("NFD", upper) if not unicodedata.combining(c)
    )

    pattern_specific = r"\b\d{4}\s*-\s*[A-Z0-9]{4,}\s*[- ]*E\s*[- ]?\s*DOCS\b"
    pattern_suffix = r"E\s*[- ]?\s*DOCS\s*[- ]*(DOCUMENTO ORIGINAL|COPIA SIMPLES)"
    pattern_url = r"E\s*[- ]?\s*DOCS\.ES\.GOV\.BR"

    patterns = [
        pattern_specific,
        pattern_suffix,
        pattern_url,
        r"E\s*[- ]?\s*DOCS\s*-\s*PAGINA",
    ]

    for pattern in patterns:
        if re.search(pattern, upper) or re.search(pattern, ascii_upper):
            return True

    keywords = ["E-DOCS", "DOCUMENTO ORIGINAL"]
    if all(kw in upper for kw in keywords):
        return True
        
    if "E DOCS" in ascii_upper and "DOCUMENTO ORIGINAL" in ascii_upper:
        e_pos = ascii_upper.find("E DOCS")
        d_pos = ascii_upper.find("DOCUMENTO ORIGINAL")
        if e_pos >= 0 and d_pos >= 0 and abs(e_pos - d_pos) <= 140:
            return True

    return False

def extract_page_text_safely(pdf_path: str, page_num: int) -> str:
    """Extrai texto de uma página PDF de forma segura."""
    if not PYMUPDF_AVAILABLE:
        return ""
    
    try:
        doc = fitz.open(pdf_path)
        if page_num < len(doc):
            page = doc.load_page(page_num)
            text = page.get_text("text")
            doc.close()
            return text
        doc.close()
    except Exception as e:
        logging.error(f"Erro ao extrair texto da página {page_num + 1} de {pdf_path}: {e}")
    
    return ""

def detect_edocs_pages_in_pdf(pdf_path: str) -> list:
    """Detecta todas as páginas de um PDF que contêm marcação E-DOCS."""
    edocs_pages = []

    if not PYMUPDF_AVAILABLE:
        logging.warning("PyMuPDF não disponível para detecção de E-DOCS")
        return edocs_pages

    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)

        for page_num in range(total_pages):
            try:
                page = doc.load_page(page_num)
                page_text = page.get_text("text") or page.get_text()
                if not page_text and hasattr(page, "get_text"):
                    page_text = page.get_text("blocks")
                    if isinstance(page_text, (list, tuple)):
                        page_text = " ".join(block[4] if len(block) > 4 else "" for block in page_text)
                page_text = (page_text or "").strip()
            except Exception as e:
                logging.debug(f"Erro ao extrair texto da página {page_num + 1}: {e}")
                page_text = ""

            if not page_text:
                continue

            if detect_edocs_marking(page_text):
                edocs_pages.append(page_num)
                
        doc.close()
    except Exception as e:
        logging.error(f"Erro ao processar PDF para detecção de E-DOCS: {e}")

    return edocs_pages

def find_edocs_marker_rect(page):
    """Localiza a coordenada exata da palavra e cria uma régua de corte cruzando a página inteira."""
    if not PYMUPDF_AVAILABLE:
        return None

    try:
        found = page.search_for("E-DOCS")
        if not found:
            found = page.search_for("DOCUMENTO ORIGINAL")
        
        if not found:
            return None

        bbox = found[0]
        page_rect = page.rect
        padding = 45.0 

        if bbox.width < bbox.height:
            x0 = max(0, bbox.x0 - padding)
            x1 = min(page_rect.x1, bbox.x1 + padding)
            return fitz.Rect(x0, 0, x1, page_rect.y1)
        else:
            y0 = max(0, bbox.y0 - padding)
            y1 = min(page_rect.y1, bbox.y1 + padding)
            return fitz.Rect(0, y0, page_rect.x1, y1)

    except Exception as e:
        logging.error(f"Erro ao localizar marcador E-DOCS: {e}")
        return None


def debug_edocs_detection(pdf_path: str, page_num: int = None) -> dict:
    """Função de debug para testar a detecção de E-DOCS em uma página específica."""
    if not PYMUPDF_AVAILABLE:
        return {"error": "PyMuPDF não disponível"}
    
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        
        if page_num is not None:
            pages_to_test = [page_num] if 0 <= page_num < total_pages else []
        else:
            pages_to_test = list(range(total_pages))
        
        results = {
            "pdf_name": os.path.basename(pdf_path),
            "total_pages": total_pages,
            "pages_tested": len(pages_to_test),
            "edocs_found": [],
            "suspicious_pages": [],
            "page_details": {}
        }
        
        for pno in pages_to_test:
            page_text = extract_page_text_safely(pdf_path, pno)
            
            page_info = {
                "page_num": pno + 1,
                "text_length": len(page_text),
                "text_preview": page_text[:300] if page_text else "",
                "edocs_detected": False,
                "suspicious_keywords": []
            }
            
            if page_text:
                suspicious_keywords = ['E-DOCS', 'EDOCS', 'CÓPIA', 'SIMPLES', 'PÁGINA']
                found_keywords = [kw for kw in suspicious_keywords if kw in page_text.upper()]
                page_info["suspicious_keywords"] = found_keywords
                
                if found_keywords:
                    results["suspicious_pages"].append(pno + 1)
                
                if detect_edocs_marking(page_text):
                    page_info["edocs_detected"] = True
                    results["edocs_found"].append(pno + 1)
            
            results["page_details"][pno + 1] = page_info
        
        doc.close()
        return results
        
    except Exception as e:
        return {"error": f"Erro ao processar PDF: {e}"}

# ==========================================
# NAVEGADOR DE DIRETÓRIOS (TUI)
# ==========================================
def navigate_and_choose_directory(start_dir="."):
    current_dir = os.path.abspath(start_dir)
    status_history = []
    
    while True:
        os.system('clear' if os.name == 'posix' else 'cls')
        
        CONSOLE.print(Panel("[bold cyan]SELECIONE A PASTA DE DESTINO[/bold cyan]", border_style="cyan"))
        CONSOLE.print(f"📂 [bold yellow]Pasta atual:[/bold yellow] [dim]{current_dir}[/dim]\n")
        
        try:
            items = sorted(os.listdir(current_dir))
            dirs = [d for d in items if os.path.isdir(os.path.join(current_dir, d)) and not d.startswith('.')]
        except PermissionError:
            status_history = ["[bold red][ERRO][/bold red] Sem permissão para acessar esta pasta."]
            current_dir = os.path.dirname(current_dir)
            continue

        table = Table(
            show_header=True, 
            header_style="bold magenta", 
            box=None,
            padding=(0, 1)
        )
        table.add_column("Opção", style="bold yellow", justify="right")
        table.add_column("Diretório", style="bold white")
        table.add_row("[0]", "[bold green]✅ SALVAR NESTA PASTA[/bold green]")
        table.add_row("[1]", "[bold blue]⬆️  Subir um nível (..)[/bold blue]")
        
        for i, d in enumerate(dirs, start=2):
            table.add_row(f"[{i}]", f"📁 [cyan]{d}/[/cyan]")
            
        CONSOLE.print(table)
        
        if status_history:
            CONSOLE.print()
            for status in status_history:
                CONSOLE.print(status)
                
        CONSOLE.print("\n[bold red][Q + ENTER] Voltar[/bold red]")
        CONSOLE.print("\nEscolha uma opção ou digite o caminho da pasta: ", end="")
        
        # Limpa espaços e aspas, mas preserva maiúsculas e minúsculas
        choice = input().strip().strip("'").strip('"')
        
        if choice == '\x1b' or choice.lower() == 'q':
            return None
        elif choice == '0':
            return current_dir
        elif choice == '1':
            current_dir = os.path.dirname(current_dir)
            status_history.clear()
        else:
            # Se não for número, tenta interpretar como caminho de diretório
            if not choice.isdigit():
                if os.path.isdir(choice):
                    current_dir = os.path.abspath(choice)
                    status_history.clear()
                else:
                    status_history = ["[bold red][ERRO][/bold red] Caminho inválido ou a pasta não existe."]
            else:
                try:
                    idx = int(choice)
                    if 2 <= idx < len(dirs) + 2:
                        current_dir = os.path.join(current_dir, dirs[idx - 2])
                        status_history.clear()
                    else:
                        status_history = ["[bold red][ERRO][/bold red] Opção inválida."]
                except ValueError:
                    status_history = ["[bold red][ERRO][/bold red] Entrada vazia ou inválida."]

# ==========================================
# TEXT USER INTERFACE (TUI) PARA TERMUX
# ==========================================

def parse_indices(input_str: str, max_val: int) -> list:
    indices = set()
    for part in input_str.split(','):
        part = part.strip()
        if not part: continue
        if '-' in part:
            try:
                s, e = map(int, part.split('-'))
                indices.update(range(min(s, e), max(s, e) + 1))
            except ValueError:
                pass
        else:
            try:
                indices.add(int(part))
            except ValueError:
                pass
    return sorted([i for i in indices if 0 <= i < max_val])

class TermuxPDFEditor:
    def __init__(self):
        self.pages_ordered = []
        self.pages_with_edocs = set()
        self.pages_with_signed_mark = set()

    def clear_screen(self):
        os.system('clear' if os.name == 'posix' else 'cls')

    def display_header(self):
        self.clear_screen()
        CONSOLE.print(Panel(
            "[bold cyan]EDITOR DE PDF PARA E-DOCS V1.0.2[/bold cyan]",
            border_style="bold blue",
            padding=(0, 2)
        ))

    def print_menu(self):
        table = Table(
            show_header=False, 
            box=None, 
            padding=(0, 1)
        )
        table.add_column("Opção", style="bold cyan", justify="right")
        table.add_column("Descrição")
        
        table.add_row("[1]", "📄 Adicionar Arquivos PDF")
        table.add_row("[2]", "📑 Ordenação Atual das Páginas")
        table.add_row("[3]", "🔀 Reordenar Páginas")
        table.add_row("[4]", "❌ Remover Páginas")
        table.add_row("[5]", "🧹 Limpar Tudo")
        table.add_row("[6]", "💾 Salvar PDF")
        
        CONSOLE.print(Panel(table, title="[bold yellow]MENU PRINCIPAL[/bold yellow]", border_style="dim white"))
        CONSOLE.print("\n[bold red][Q + ENTER] Sair do sistema[/bold red]")

    def run(self):
        if not PYMUPDF_AVAILABLE or not PYPDF_AVAILABLE:
            print("\n[AVISO] Bibliotecas essenciais ausentes. O script pode falhar.")
            input("Pressione ENTER para continuar mesmo assim...")

        while True:
            self.display_header()
            CONSOLE.print(f"Páginas na fila: [bold green]{len(self.pages_ordered)}[/bold green]\n")
            self.print_menu()
            CONSOLE.print("\nEscolha uma opção: ", end="")
            choice = input().strip()
            
            if choice == '1':
                self.add_pdf()
            elif choice == '2':
                status_history = []
                while True:
                    self.list_pages(title="ORDENAÇÃO ATUAL DAS PÁGINAS")
                    if status_history:
                        CONSOLE.print()
                        for st in status_history: CONSOLE.print(st)
                    CONSOLE.print("\n[bold red][Q + ENTER] Voltar[/bold red]")
                    inp = input("\nEscolha uma opção: ").strip()
                        
                    if inp == '\x1b' or inp.lower() == 'q':
                        break
            elif choice == '3':
                self.move_page()
            elif choice == '4':
                self.remove_page()
            elif choice == '5':
                self.pages_ordered.clear()
                self.pages_with_edocs.clear()
                self.pages_with_signed_mark.clear()
                while True:
                    self.display_header()
                    CONSOLE.print("\n[bold green][OK] Todos os documentos foram removidos com sucesso.[/bold green]")
                    CONSOLE.print("\n[bold red][Q + ENTER] Voltar[/bold red]")
                    inp = input("\nEscolha uma opção: ").strip()
                        
                    if inp == '\x1b' or inp.lower() == 'q':
                        break
            elif choice == '6':
                self.merge_and_save()
            elif choice == '0' or choice == '\x1b' or choice.lower() == 'q':
                self.clear_screen()
                break

    def add_pdf(self):
        current_dir = os.path.abspath(".")
        status_history = [] 
        
        while True:
            self.clear_screen()
            CONSOLE.print(Panel("[bold cyan]ADICIONAR ARQUIVOS PDF[/bold cyan]", border_style="cyan"))
            CONSOLE.print(f"📂 [bold yellow]Pasta atual:[/bold yellow] [dim]{current_dir}[/dim]\n")
            
            try:
                items = sorted(os.listdir(current_dir))
                dirs = [d for d in items if os.path.isdir(os.path.join(current_dir, d)) and not d.startswith('.')]
                pdfs = [f for f in items if os.path.isfile(os.path.join(current_dir, f)) and f.lower().endswith('.pdf')]
            except PermissionError:
                status_history.append("[bold red][ERRO][/bold red] Sem permissão para acessar esta pasta.")
                current_dir = os.path.dirname(current_dir)
                continue

            table = Table(
                show_header=True, 
                header_style="bold magenta", 
                box=None,
                padding=(0, 1)
            )
            table.add_column("Item", style="bold yellow", justify="right")
            table.add_column("Nome")
            table.add_row("[0]", "[bold blue]⬆️  Subir um nível (..)[/bold blue]")
            
            idx = 1
            dir_map = {}
            for d in dirs:
                table.add_row(f"[{idx}]", f"📁 [cyan]{d}/[/cyan]")
                dir_map[idx] = d
                idx += 1
                
            pdf_map = {}
            for p in pdfs:
                table.add_row(f"[{idx}]", f"📄 [bold green]{p}[/bold green]")
                pdf_map[idx] = p
                idx += 1
                
            CONSOLE.print(table)
            
            if status_history:
                CONSOLE.print()
                for status in status_history:
                    CONSOLE.print(status)
            
            CONSOLE.print("\n[bold red][Q + ENTER] Voltar[/bold red]")    
            CONSOLE.print("\nDigite uma opção ou caminho: ", end="")
            choice = input().strip().strip("'").strip('"')
            
            if choice == '\x1b' or choice.lower() == 'q':
                return
                
            selected_pdf = None
            if not choice.isdigit():
                if os.path.exists(choice) and choice.lower().endswith('.pdf'):
                    selected_pdf = os.path.abspath(choice)
                else:
                    status_history.append("[bold red][ERRO][/bold red] Caminho inválido ou não é um PDF.")
                    continue
            else:
                try:
                    choice_idx = int(choice)
                    if choice_idx == 0:
                        current_dir = os.path.dirname(current_dir)
                        continue
                    elif choice_idx in dir_map:
                        current_dir = os.path.join(current_dir, dir_map[choice_idx])
                        continue
                    elif choice_idx in pdf_map:
                        selected_pdf = os.path.join(current_dir, pdf_map[choice_idx])
                    else:
                        status_history.append("[bold red][ERRO][/bold red] Opção inválida.")
                        continue
                except ValueError:
                    status_history.append("[bold red][ERRO][/bold red] Entrada inválida ou vazia.")
                    continue

            if selected_pdf:
                CONSOLE.print(f"\n[cyan]Processando arquivo:[/cyan] {os.path.basename(selected_pdf)} ...")
                    
                try:
                    pdf_path, clean_stats = clean_pdf_signatures(selected_pdf)
                    doc = fitz.open(pdf_path)
                    edocs_pages = detect_edocs_pages_in_pdf(pdf_path)
                    signed_pages = detect_signed_pages_in_pdf(pdf_path)
                    
                    for pno in range(doc.page_count):
                        self.pages_ordered.append((pdf_path, pno))
                        if pno in edocs_pages:
                            self.pages_with_edocs.add((pdf_path, pno))
                        if pno in signed_pages:
                            self.pages_with_signed_mark.add((pdf_path, pno))
                    
                    doc.close()
                    
                    # Monta a mensagem de sucesso principal
                    base_msg = f"[bold green][OK] PDF adicionado: {os.path.basename(selected_pdf)}. E-DOCS: {len(edocs_pages)} | Assinaturas: {len(signed_pages)}[/bold green]"
                    
                    # Anexa o feedback do pré-tratamento se algo foi alterado
                    if clean_stats.get("error"):
                        base_msg += f"\n[bold yellow]Aviso no pré-tratamento:[/bold yellow] {clean_stats['error']}"
                    elif any([clean_stats["acroform_removed"], clean_stats["perms_removed"], clean_stats["sigflags_removed"], clean_stats["widgets_flattened"] > 0]):
                        details = []
                        if clean_stats["acroform_removed"]: details.append("AcroForm")
                        if clean_stats["perms_removed"]: details.append("Permissões")
                        if clean_stats["sigflags_removed"]: details.append("SigFlags")
                        
                        msg_parts = []
                        if details:
                            msg_parts.append(f"Removidos: {', '.join(details)}")
                        if clean_stats["widgets_flattened"] > 0:
                            msg_parts.append(f"Widgets achatados: {clean_stats['widgets_flattened']}")
                            
                        base_msg += f"\n[dim]Limpeza pré-importação: {' | '.join(msg_parts)}[/dim]"
                        
                    status_history.append(base_msg)
                except Exception as e:
                    status_history.append(f"[bold red][ERRO] Falha ao processar o arquivo: {e}[/bold red]")

    def list_pages(self, title="ORDENAÇÃO ATUAL DAS PÁGINAS"):
        self.clear_screen()
        CONSOLE.print(Panel(f"[bold cyan]{title}[/bold cyan]", border_style="cyan"))
            
        if not self.pages_ordered:
            print("\nNenhuma página carregada na fila no momento.")
            return

        table = Table(title="[bold yellow]PÁGINAS NA FILA[/bold yellow]", border_style="dim white")
        table.add_column("#", style="bold cyan", justify="right", width=5)
        table.add_column("Arquivo Original", style="bold white")
        table.add_column("Pág.", justify="center", width=6)
        table.add_column("Marcações/Status", justify="left")

        for i, (path, pno) in enumerate(self.pages_ordered):
            fname = get_display_name(path)
            tags = []
            if (path, pno) in self.pages_with_edocs:
                tags.append("[bold green][E-DOCS][/bold green]")
            if (path, pno) in self.pages_with_signed_mark:
                tags.append("[bold yellow][ASSINATURA][/bold yellow]")
                
            tag_str = " ".join(tags) if tags else "[dim]—[/dim]"
            table.add_row(f"[{i}]", fname, str(pno + 1), tag_str)

        CONSOLE.print(table)

    def move_page(self):
        status_history = []
        while True:
            self.list_pages(title="REORDENAR PÁGINAS")
            if not self.pages_ordered:
                CONSOLE.print("\n[bold red][Q + ENTER] Voltar[/bold red]")
                inp = input("\nEscolha uma opção: ").strip()
                if inp == '\x1b' or inp.lower() == 'q': return
                continue

            if status_history:
                CONSOLE.print()
                for st in status_history: CONSOLE.print(st)
            CONSOLE.print("\n[bold red][Q + ENTER] Voltar[/bold red]")
            CONSOLE.print("\nDigite o número ou intervalo do índice (#) das páginas que deseja reordenar: ", end="")
                
            inp_src = input().strip()
            
            if inp_src == '\x1b' or inp_src.lower() == 'q': return
                
            src_indices = parse_indices(inp_src, len(self.pages_ordered))
            if not src_indices:
                status_history = ["[bold red][ERRO][/bold red] Nenhuma página válida selecionada."]
                continue
                
            CONSOLE.print("Digite a nova posição (#) de destino para esse bloco: ", end="")
                
            dst_str = input().strip()
            if dst_str == '\x1b' or dst_str.lower() == 'q': return
            
            try:
                dst = int(dst_str)
                items_to_move = [self.pages_ordered[i] for i in src_indices]
                remaining = [item for i, item in enumerate(self.pages_ordered) if i not in src_indices]
                
                if dst < 0: dst = 0
                if dst > len(remaining): dst = len(remaining)
                
                self.pages_ordered = remaining[:dst] + items_to_move + remaining[dst:]
                status_history = [f"[bold green][OK][/bold green] {len(items_to_move)} página(s) movida(s) para a posição [{dst}]."]
            except ValueError:
                status_history = ["[bold red][ERRO][/bold red] Posição de destino inválida."]

    def remove_page(self):
        status_history = []
        while True:
            self.list_pages(title="REMOVER PÁGINAS")
            if not self.pages_ordered:
                CONSOLE.print("\n[bold red][Q + ENTER] Voltar[/bold red]")
                inp = input("\nEscolha uma opção: ").strip()
                if inp == '\x1b' or inp.lower() == 'q': return
                continue

            if status_history:
                CONSOLE.print()
                for st in status_history: CONSOLE.print(st)
            CONSOLE.print("\n[bold red][Q + ENTER] Voltar[/bold red]")
            CONSOLE.print("\nDigite o número ou intervalo do índice (#) das páginas que deseja remover: ", end="")
                
            inp = input().strip()
            
            if inp == '\x1b' or inp.lower() == 'q': return
                
            indices = parse_indices(inp, len(self.pages_ordered))
            if not indices:
                status_history = ["[bold red][ERRO][/bold red] Nenhuma página válida selecionada."]
            else:
                for idx in sorted(indices, reverse=True):
                    path, pno = self.pages_ordered.pop(idx)
                    self.pages_with_edocs.discard((path, pno))
                    self.pages_with_signed_mark.discard((path, pno))
                    
                status_history = [f"[bold green][OK][/bold green] {len(indices)} página(s) removida(s) com sucesso."]

    def merge_and_save(self):
        if not self.pages_ordered:
            while True:
                self.display_header()
                CONSOLE.print("\n[bold red][ERRO] Nenhuma página carregada para salvar.[/bold red]")
                CONSOLE.print("\n[bold red][Q + ENTER] Voltar[/bold red]")
                inp = input("\nEscolha uma opção: ").strip()
                if inp == '\x1b' or inp.lower() == 'q': return
            return
            
        chosen_dir = navigate_and_choose_directory()
        if not chosen_dir:
            return
            
        status_history = []
        while True:
            self.display_header()
            CONSOLE.print(f"📂 [bold yellow]Pasta selecionada:[/bold yellow] [dim]{chosen_dir}[/dim]")
            
            if status_history:
                CONSOLE.print()
                for st in status_history: CONSOLE.print(st)
                
            CONSOLE.print("\n[bold red][Q + ENTER] Voltar[/bold red]")
            CONSOLE.print("\nDigite o nome do arquivo final: ", end="")
            
            filename = input().strip()
            
            if filename == '\x1b' or filename.lower() == 'q':
                return
            if not filename:
                status_history = ["[bold red][ERRO][/bold red] O nome do arquivo não pode ser vazio."]
                continue
                
            if not filename.lower().endswith('.pdf'):
                filename += '.pdf'
                
            out_path = os.path.join(chosen_dir, filename)
                
            CONSOLE.print(f"\n[cyan]Processando documento e gerando:[/cyan] {filename} ...")
                
            writer = PdfWriter()
            edocs_count = 0
            signed_count = 0
            
            try:
                for file_path, pno in self.pages_ordered:
                    key = (file_path, pno)
                    if PYMUPDF_AVAILABLE and key in self.pages_with_signed_mark:
                        src = fitz.open(file_path)
                        page0 = src.load_page(pno)
                        image_only_pdf = build_single_page_image_pdf_bytes(page0, dpi=300)
                        writer.append(io.BytesIO(image_only_pdf))
                        signed_count += 1
                        src.close()
                    elif PYMUPDF_AVAILABLE and key in self.pages_with_edocs:
                        src = fitz.open(file_path)
                        single = fitz.open()
                        single.insert_pdf(src, from_page=pno, to_page=pno)
                        
                        # Limpa metadados internos da página
                        single.del_xml_metadata()
                        
                        if shift_edocs_stamp_left_in_page(single[0], dx_pts=EDOCS_SHIFT_LEFT_PTS):
                            edocs_count += 1
                        
                        buf = io.BytesIO()
                        single.save(buf, garbage=4, deflate=True, clean=True, expand=255, pretty=False, no_new_id=True)
                        buf.seek(0)
                        writer.append(buf)
                        single.close()
                        src.close()
                    else:
                        writer.append(file_path, pages=(pno, pno + 1))
                        
                writer.add_metadata({
                    '/Creator': 'Scanner', 
                    '/Producer': 'Generic',
                    '/Author': '',
                    '/Title': ''
                })
                # Remove qualquer vestígio de histórico de IDs para conformidade com a versão gráfica
                writer._ID = None
                
                with open(out_path, 'wb') as f:
                    writer.write(f)
                    
                while True:
                    self.display_header()
                    CONSOLE.print(f"📂 [bold yellow]Pasta selecionada:[/bold yellow] [dim]{chosen_dir}[/dim]\n")
                    CONSOLE.print(f"[bold green][SUCESSO][/bold green] PDF salvo em: [cyan]{out_path}[/cyan]")
                    CONSOLE.print(f"-> E-DOCS reajustados: [bold white]{edocs_count}[/bold white]")
                    CONSOLE.print(f"-> Assinaturas rasterizadas: [bold white]{signed_count}[/bold white]")
                    CONSOLE.print("\n[bold red][Q + ENTER] Voltar[/bold red]")
                    
                    inp = input("\nEscolha uma opção: ").strip()
                        
                    if inp == '\x1b' or inp.lower() == 'q':
                        return

            except Exception as e:
                status_history = [f"[bold red][ERRO][/bold red] Falha ao salvar: {e}"]
                continue

if __name__ == '__main__':
    app = TermuxPDFEditor()
    app.run()
