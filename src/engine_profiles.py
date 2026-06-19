# src/engine_profiles.py

ENGINE_PROFILES = {
    "chatgpt": {
        "url": "https://chatgpt.com",
        "input_selector": '#prompt-textarea, [contenteditable="true"]',
        "response_selector": "[data-message-author-role='assistant'], article",
        "send_button_selector": 'button[data-testid="send-button"]',
        "login_wall_indicators": ['data-testid="login-button"', 'Log in to ChatGPT', 'Sign up'],
        "tiny_prompt": "hi",
        "response_timeout_sec": 90,
        "stability_threshold": 6,  # 3 seconds of no change
        "injection_method": "fill" # ChatGPT prefers Playwright native fill
    },
    "perplexity": {
        "url": "https://www.perplexity.ai",
        "input_selector": 'textarea, [contenteditable="true"]',
        "response_selector": "div.prose, div.default.font-sans",
        "send_button_selector": 'button[aria-label*="Submit"]',
        "login_wall_indicators": ['Sign in', 'Sign up', 'restricted'],
        "tiny_prompt": "hi",
        "response_timeout_sec": 120,
        "stability_threshold": 6,
        "injection_method": "hardware" # Perplexity needs raw hardware keystrokes
    },
    "gemini": {
        "url": "https://gemini.google.com/app",
        "input_selector": 'rich-textarea, div[contenteditable="true"], textarea:visible',
        "response_selector": "message-content, .message-content, div.message-text",
        "send_button_selector": 'button[aria-label*="Send"]',
        "login_wall_indicators": ['Sign in', 'Create account', 'requires authentication'],
        "tiny_prompt": "hi",
        "response_timeout_sec": 120,
        "stability_threshold": 6,
        "injection_method": "hardware" # Gemini needs raw hardware keystrokes
    }
}