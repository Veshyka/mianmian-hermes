#!/usr/bin/env python3
"""量本地/云端 VLM 的识图质量：一张图 + 一份 ground truth → CER、准确率、数字命中率。

用法：
  # PDF 页面：图由 PDF 渲染，GT 取文本层
  python vlm_page_bench.py --pdf /path/doc.pdf --page 5 --dpi 150 \
      --endpoint http://172.17.0.1:8083/v1/chat/completions --model minicpm-v4.6:1b

  # 已有图片 + GT 文本
  python vlm_page_bench.py --img /path/x.png --gt-file /path/x.txt --endpoint <url> --model <m>

  # 定向提问（表格/截图类必做，幻觉往往只在这里暴露）
  python vlm_page_bench.py --img x.png --gt-file x.txt --ask "图中型号为 X 的设备，每一行的重量和编号分别是多少？"

关键点（踩过的）：
  * 本地思考型 VLM 的正文可能全被 reasoning_content 吃掉 → 默认 max_tokens 给 4096，
    并用 --no-think 发 chat_template_kwargs.enable_thinking=false（llama.cpp 实测有效，不用改服务端）。
  * 打分前先剥掉模型擅自加的 <table><tr><td> 标签，否则 CER 被抬高一倍多。
  * 只要涉及数字/时间戳/路径/人名，看「数字命中率」而不是 CER。
"""
import argparse
import base64
import difflib
import json
import re
import time
import urllib.request
from collections import Counter

PROMPT_TR = "请逐字转录图中的全部文字，保持阅读顺序，不要翻译、不要总结、不要加任何解释。"
# urllib 默认读环境代理；本地端点/跨网桥地址必须绕开代理，否则 curl 能通但脚本不通
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ask(endpoint, model, img_path, prompt, max_tokens=4096, no_think=False, timeout=600):
    b64 = base64.b64encode(open(img_path, 'rb').read()).decode()
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        ]}],
    }
    if no_think:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    t0 = time.time()
    with _OPENER.open(req, timeout=timeout) as r:
        resp = json.loads(r.read())
    m = resp['choices'][0]['message']
    return {'content': m.get('content') or '',
            'reasoning_len': len(m.get('reasoning_content') or ''),
            'finish': resp['choices'][0]['finish_reason'],
            'usage': resp.get('usage'), 'sec': round(time.time() - t0, 2)}


def norm(t):
    t = re.sub(r'[\s\u3000]+', '', t)
    t = re.sub(r'</?t[d|r]?>|</>', '', t)      # 模型擅自加的表格标签
    t = t.replace('<table>', '').replace('</table>', '')
    for a, b in [('（', '('), ('）', ')'), ('，', ','), ('。', '.'), ('：', ':'), ('；', ';')]:
        t = t.replace(a, b)
    return t


def score(gt, out):
    a, b = norm(gt), norm(out)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ins = dele = rep = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'insert':
            ins += j2 - j1
        elif tag == 'delete':
            dele += i2 - i1
        elif tag == 'replace':
            rep += max(i2 - i1, j2 - j1)
    err = ins + dele + rep
    cg, cv = Counter(re.findall(r'\d+', gt)), Counter(re.findall(r'\d+', out))
    num_gt = sum(cg.values())
    num_hit = sum((cg & cv).values())
    return {'gt_chars': len(a), 'out_chars': len(b), 'ins': ins, 'del': dele, 'rep': rep,
            'err': err, 'CER': round(err / max(len(a), 1), 4), 'acc': round(1 - err / max(len(a), 1), 4),
            'num_gt': num_gt, 'num_hit': num_hit,
            'num_rate': round(num_hit / num_gt, 4) if num_gt else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--endpoint', default='http://172.17.0.1:8083/v1/chat/completions')
    ap.add_argument('--model', default='minicpm-v4.6:1b')
    ap.add_argument('--img')
    ap.add_argument('--pdf')
    ap.add_argument('--page', type=int, default=0)
    ap.add_argument('--dpi', type=int, default=150)
    ap.add_argument('--gt-file')
    ap.add_argument('--ask', default=PROMPT_TR)
    ap.add_argument('--max-tokens', type=int, default=4096)
    ap.add_argument('--no-think', action='store_true')
    a = ap.parse_args()

    img, gt = a.img, ''
    if a.pdf:
        import pymupdf                        # 别用 fitz 旧别名
        doc = pymupdf.open(a.pdf)
        page = doc[a.page]
        img = '/tmp/vlm_bench_p%d_%ddpi.png' % (a.page, a.dpi)
        page.get_pixmap(dpi=a.dpi).save(img)
        gt = page.get_text()
    if a.gt_file:
        gt = open(a.gt_file, encoding='utf-8').read()
    if not img:
        ap.error('需要 --img 或 --pdf')

    r = ask(a.endpoint, a.model, img, a.ask, a.max_tokens, a.no_think)
    print('img=%s  sec=%s  finish=%s  reasoning_len=%d  content_len=%d'
          % (img, r['sec'], r['finish'], r['reasoning_len'], len(r['content'])))
    if not r['content'].strip():
        print('!! content 为空——思考型 VLM 的推理段吃光了预算：加 --max-tokens 或 --no-think')
    print('---- 模型输出 ----')
    print(r['content'])
    if gt:
        s = score(gt, r['content'])
        print('---- 评分（对 GT）----')
        print(json.dumps(s, ensure_ascii=False))
        print('CER=%(CER).3f acc=%(acc).3f  数字命中 %(num_hit)d/%(num_gt)d' % s)
        if s['num_rate'] is not None and s['num_rate'] < 0.95:
            print('!! 数字命中率 <95%%：涉及数值/时间戳/路径的场景不可用（CER 好看也没用）')


if __name__ == '__main__':
    main()
