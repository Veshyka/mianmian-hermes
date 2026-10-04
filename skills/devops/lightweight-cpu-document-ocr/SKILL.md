---
name: lightweight-cpu-document-ocr
description: Use when 扫描件PDF/整页图片需中文OCR（纯CPU、内存紧）。
---

# 轻量文档 OCR（纯 CPU，中文扫描件）

## 何时用
- 扫描件/图片型 PDF，`read_file`、pymupdf、pypdfium2 抽不出文字（可提取字符数≈0）
- 需要整页版面还原：文字全、阅读顺序对、表格别散
- 机器没有可用显存，或内存紧张（单页峰值要 <1GB）

不适用：日常发图问「这是什么」、表情包 —— 那些交给视觉模型，不要走 OCR。

## 结论（本机实测，i3-12100F / 15.8GB）
**RapidLayout（版面，ONNX）+ RapidOCR（识别，ONNX）**，纯 CPU、无 torch/paddle、不碰显存。
单页 A4 2.2~4.0 s，峰值内存 715~818 MB，中文整页字符准确率 100%，双栏阅读顺序正确。

## 安装（uv，无 pip）
```bash
uv venv /opt/data/ocr-eval/venv --python 3.11
VIRTUAL_ENV=/opt/data/ocr-eval/venv HTTP_PROXY=http://172.17.0.1:17890 HTTPS_PROXY=http://172.17.0.1:17890 \
  uv pip install -i https://pypi.tuna.tsinghua.edu.cn/simple rapidocr rapid-layout rapid-table pypdfium2
```
RapidOCR 的 PP-OCRv6 模型随包自带（约 31MB），无需下载。RapidLayout 模型首次运行需联网（走代理）。

## 调用
```bash
cd /opt/data/ocr-eval && HTTP_PROXY=http://172.17.0.1:17890 HTTPS_PROXY=http://172.17.0.1:17890 \
  /opt/data/ocr-eval/venv/bin/python ocr_doc.py <input.pdf|img> <out.md>
```
脚本在 `/opt/data/ocr-eval/ocr_doc.py`，小字密集可加 `--dpi 200`。

## 流水线要点（照做，别简化）
1. RapidLayout 出**版面块**；RapidOCR **整页只跑一次**（逐块裁切会重复检测，实测慢 5 倍：14s → 2.5s）。
2. 按「文本行中心点落入哪个块」归块，取面积最小的包含块。
3. **块排序用「块内首行的实际 y」，不要用版面框的 y0** —— 标题/副标题框的 y0 会判反。
4. 阅读顺序：按 x 区间重叠（>40%）把块聚成栏，栏按 x 排序、栏内按首行 y 排序。
5. 表格：块内按 y 中心分桶当行（容差≈0.6 倍行高），行内按 x 排序。

## 坑（都踩过）
- **别自己写栏位聚类判双栏**：x 最大间隙法能修好双栏，但会把**单栏页面的表格误判成双栏**，按列重排把表格弄坏。必须用真正的版面模型。
- **PP-StructureV3 峰值 3.5~6.2 GB、单页 11~26 s**，且实测把标题/副标题当页眉**丢掉**；lean 配置（小版面模型+关可选模块）会丢掉全部正文段落（字符覆盖仅 36%）。全功能版在这类机器上不划算。
- RapidLayout 的 boxes 可能是 **XYXY 平铺数组**（不是四点 polygon），解析前先判断 ndim。
- 不测某方案就写「未实测」，别拿文档数字冒充实测。

## 验收方式
- 图片型 PDF 要先用 pypdfium2 验证 `get_text_range()` 长度为 0，确认真的是无文字层。
- 测试图要覆盖：单栏正文+表格、**双栏**（查阅读顺序）、高 dpi 带噪点倾斜（模拟真实扫描）。
- 准确率用 **bag-of-chars** 算（不计顺序），阅读顺序单独人工核验 —— LCS 在「内容全对但顺序不同」时会误判。
