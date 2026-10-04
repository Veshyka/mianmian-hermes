# PDF 进 Hermes 与 OCR 兜底（实测）

## 入口行为

| 输入 | 路径 | 说明 |
|---|---|---|
| 有文字层的 PDF | `read_file` 直接抽 | 不用任何模型，最快 |
| 扫描件 / 被判需 OCR 的页 | `ocr_pdf.py`（MinerU 3.4.5，纯 CPU） | 只对扫描页调 OCR |
| 图片 | 视觉模型 | 不走 OCR |

## `read_file` 会把有文字层的 PDF 误判成「需 OCR」并整份拒绝

结果里出现这种提示就是它干的：

```
[NEEDS OCR: pages N-M of this PDF are scanned images with no text layer — their content is MISSING below.]
```

它靠 anydoc 的扫描页信号，**实测把各有一千多字文本层的页也标上了**（19 页学术 PDF 的 10~11 页）。

- **判真伪**：`pymupdf` 数那几页字符数（`len(page.get_text())`）——几百字以上且看着像真内容就是误报。
- **兜底一行**：
  ```bash
  cd /opt/data/ocr-eval && ./venv-mineru3/bin/python ocr_pdf.py <in.pdf> -o <outdir> [--force-ocr --pages N-M]
  ```
  它会先打印「共 N 页：文字层 x / 需 OCR y」——有文字层就走快路（实测 19 页 1.1 s 出 md，不加载模型）；开关另有 `--min-text`（多少字算有文字层）/ `--lang ch` / `--no-server`。
- 别把这条提示当成文件损坏或 read_file 坏了；它只说明那几页**在它眼里**没文字层。

## 选型数据在哪（不在这里重复）

扫描件整页 OCR 的方案对比、安装、坑：用户自己的两个技能 `scanned-pdf-ocr`（**以它为准**：MinerU 3.4.5 `-b pipeline`，2.4~2.8 GB / 3 s 每页 / 100% 字符准确率）与 `lightweight-cpu-document-ocr`（RapidLayout+RapidOCR 轻量路线）。

⚠️ `lightweight-cpu-document-ocr` 的**结论已过时**：它把 RapidLayout+RapidOCR 当作文档级方案，而实测 RapidOCR 没有版面分析（双栏输出按「左1→右1→左2」交错、表格拆成孤立单元格文本），只适合「已有文字层、只想抠几个字」。真做文档还原用 `scanned-pdf-ocr` 那条。
