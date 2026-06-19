import redis
import json

# Connect to the local Redis server
r = redis.Redis(host='127.0.0.1', port=6379)

# Create a test task
task_payload = {
    "task_id": "test_101",
    "engine": "chatgpt",
    "prompt": "What is the current stock price of Apple and what are the recent news articles about it?"
}

# Push the task into the queue (the exact queue name main.py is listening to)
r.lpush("traqer_tasks", json.dumps(task_payload))
print("Task successfully pushed to Redis!")