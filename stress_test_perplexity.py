import redis
import json
import hashlib

def run_stress_test():
    r = redis.Redis(host='127.0.0.1', port=6379)
    engine = "perplexity"
    prompts = [
        "Explain the impact of quantum computing on cybersecurity.",
        "What are the top 5 emerging technology trends in 2026?"
    ]
    
    print(f"[TEST] Ingesting prompts strictly for {engine.upper()}...\n")
    
    total_queued = 0
    for prompt in prompts:
        task_id = hashlib.md5(f"{engine}_{prompt}".encode()).hexdigest()[:12]
        payload = {"task_id": f"task_{task_id}", "engine": engine, "prompt": prompt}
        r.lpush(f"task_queue:{engine}", json.dumps(payload))
        total_queued += 1
        print(f"[TEST] Queued -> task_queue:{engine}")
            
    print(f"\n[TEST] {total_queued} tasks queued. Watch the Perplexity Agent terminal!")

if __name__ == "__main__":
    run_stress_test()