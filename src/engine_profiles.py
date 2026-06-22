# src/engine_profiles.py

ENGINE_PROFILES = {
    "chatgpt": {
        "url": "https://chatgpt.com",
        "input_selector": '#prompt-textarea, [contenteditable="true"]',
        "response_selector": "[data-message-author-role='assistant'], article",
        "send_button_selector": 'button[data-testid="send-button"]',
        "login_wall_indicators": ['data-testid="login-button"', 'Log in to ChatGPT', 'Sign up'],
        "tiny_prompt": "hi",
        "injection_method": "fill",
        "stream_indicators": ["backend-api/conversation"]
    },
    "perplexity": {
        "url": "https://www.perplexity.ai",
        "search_url_template": "https://www.perplexity.ai/search?q={query}",
        "input_selector": '[data-lexical-editor="true"]',
        "response_selector": "div.prose, div[class*='prose'], div.break-words",
        "send_button_selector": 'button[aria-label*="Submit"], button[type="submit"]',
        "login_wall_indicators": ['Please verify you are a human', 'cf-error', 'unusual traffic', 'Access denied'],
        "tiny_prompt": "hi",
        "injection_method": "hardware",
        "stream_indicators": ["rest/thread", "rest/ask", "query", "graphql", "socket.io"]
    },
    "gemini": {
        "url": "https://gemini.google.com/app",
        "input_selector": 'rich-textarea, div[contenteditable="true"], textarea:visible',
        "response_selector": "message-content, .message-content, div.message-text",
        "send_button_selector": 'button[aria-label*="Send"]',
        "login_wall_indicators": ['Sign in to continue', 'Create account', 'requires authentication'],
        "tiny_prompt": "hi",
        "injection_method": "hardware",
        "stream_indicators": ["batchexecute", "chat"]
    }
}