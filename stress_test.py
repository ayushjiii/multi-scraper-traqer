import redis
import json
import hashlib

def run_stress_test():
    r = redis.Redis(host='127.0.0.1', port=6379)
    
    engines = ["chatgpt", "perplexity", "gemini"]
    prompts = [
        "What are the top 5 emerging technology trends in 2026?",
        "Explain the impact of quantum computing on cybersecurity.",
        "Summarize the recent financial reports for Microsoft."
    ]
    
    print("[TEST] Ingesting 3 unique prompts and fanning out across all agents...")
    
    total_queued = 0
    for prompt in prompts:
        for engine in engines:
            task_id = hashlib.md5(f"{engine}_{prompt}".encode()).hexdigest()[:12]
            
            task_payload = {
                "task_id": f"task_{task_id}",
                "engine": engine,
                "prompt": prompt
            }
            
            queue_name = f"task_queue:{engine}"
            r.lpush(queue_name, json.dumps(task_payload))
            total_queued += 1
            print(f"[TEST] Queued -> {queue_name} | Prompt: '{prompt[:30]}...'")
            
    print(f"\n[TEST] {total_queued} isolated tasks successfully pushed. Watch Orchestrator terminal!")

if __name__ == "__main__":
    run_stress_test()