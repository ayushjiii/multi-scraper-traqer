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
        # Zero-knowledge / generic selectors
        "input_selector": 'textarea, [contenteditable="true"], input[type="text"]',
        "response_selector": "main, article, div.prose, [data-testid='answer-text']",
        "send_button_selector": 'button[type="submit"], button:has(svg)',
        "login_wall_indicators": ['just a moment', 'cf-error', 'challenges.cloudflare.com'],
        "tiny_prompt": "hi",
        "injection_method": "hardware",
        "stream_indicators": ["graphql", "socket.io", "query"]
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