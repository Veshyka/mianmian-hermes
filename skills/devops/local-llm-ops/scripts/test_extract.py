#!/usr/bin/env python3
# 模型抽取质量对比测试（因果链/实体/格式）——2026-08-25 棉棉
# 用法: python3 test_extract.py <model_name>  例如 python3 test_extract.py qwen3:8b
# 输出: 事实数 / 因果边 / 实体数 / JSON 完整性 / 用时——多模型跑同文本公平对比
# 注意: 测第二个模型前先卸载当前模型（curl /api/generate -d '{"model":"...","keep_alive":0}'）防显存双加载
import json, sys, urllib.request, time

model = sys.argv[1] if len(sys.argv) > 1 else "qwen3:8b"
OLLAMA = "http://127.0.0.1:11434/api/chat"

TEXT = """8月23日，宿舍的空调坏了，维修师傅说要换压缩机，维修费要800块。小林决定先不修，改用电风扇凑合。但南京持续高温，电风扇根本扛不住，小林中暑了。于是他找舍友小张商量，小张推荐了学校的免费维修服务。学校维修队当天就修好了空调，小林很开心，还特意请小张喝了奶茶。这件事让小林意识到学校服务的重要性，后来他遇到宿舍问题都会先问小张。"""

PROMPT = f"""Extract SIGNIFICANT facts from the following Chinese text. ALL output values MUST be in Chinese (same language as input). Be SELECTIVE - only extract facts worth remembering.

For EACH fact, provide JSON with these fields:
- "what": core fact (1-2 sentences max, Chinese)
- "when": temporal info or "无"
- "where": location or "无"
- "who": people involved or "无"
- "why": significance or "无"
- "fact_type": "world" (objective facts) or "assistant"
- "entities": array of plain strings (people, places, objects, abstract concepts - anything that could help link related facts)
- "causal_relations": array of {{"target_fact_index": N, "relation_type": "caused_by"}} where N is the 0-based index of a PREVIOUS fact in your output array that CAUSED this fact. Empty array if no causal link to previous facts.

IMPORTANT: causal_relations must ONLY reference facts at earlier indexes. Extract causal chains carefully - if fact B happened because of fact A, B's causal_relations should include A's index.

Return ONLY a JSON object: {{"facts": [...]}}

Text: {TEXT}"""

def call():
    data = json.dumps({"model": model, "messages": [{"role": "user", "content": PROMPT}], "stream": False, "options": {"num_predict": 4096, "temperature": 0.2}}).encode()
    req = urllib.request.Request(OLLAMA, data=data, headers={"Content-Type": "application/json"})
    t0 = time.time()
    resp = json.loads(urllib.request.urlopen(req, timeout=300).read())
    out = resp.get("message", {}).get("content", "")
    print(f"=== {model} | 用时 {time.time()-t0:.1f}s | eval {resp.get('eval_count')} tokens ===")
    try:
        start, end = out.find("{"), out.rfind("}") + 1
        parsed = json.loads(out[start:end])
        facts = parsed.get("facts", [])
        print(f"抽取事实数: {len(facts)}")
        causal_edges = 0
        for i, f in enumerate(facts):
            cr = f.get("causal_relations") or []
            causal_edges += len(cr)
            ents = f.get("entities") or []
            print(f"  [{i}] {str(f.get('what','?'))[:48]}")
            print(f"      who={f.get('who','')} | entities={ents} | causal->{cr}")
        print(f"\n因果边总数: {causal_edges}")
        print(f"实体总数: {sum(len(f.get('entities') or []) for f in facts)}")
    except Exception as e:
        print(f"JSON 解析失败: {e}")
        print("原始输出前500:", out[:500])

call()
