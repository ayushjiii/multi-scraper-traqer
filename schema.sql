-- Profile lifecycle states actually used by the agents.
CREATE TYPE profile_status AS ENUM ('AVAILABLE', 'BUSY', 'EXPIRED');

CREATE TABLE browser_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_name VARCHAR(50) UNIQUE NOT NULL,
    engine_type VARCHAR(30) NOT NULL,
    storage_path TEXT NOT NULL,
    status profile_status DEFAULT 'AVAILABLE',
    trust_score INT DEFAULT 100,
    last_used_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    proxy_string TEXT
);

CREATE TABLE proxies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_string TEXT UNIQUE NOT NULL,
    -- Banning is per-engine: a proxy can be burned on one engine but fine on others.
    status VARCHAR(20) DEFAULT 'ACTIVE',
    chatgpt_banned BOOLEAN DEFAULT FALSE,
    perplexity_banned BOOLEAN DEFAULT FALSE,
    gemini_banned BOOLEAN DEFAULT FALSE
);

CREATE TABLE scrape_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id UUID REFERENCES browser_profiles(id) ON DELETE SET NULL,
    task_id VARCHAR(100) NOT NULL,
    engine_name VARCHAR(30) NOT NULL,
    input_prompt TEXT NOT NULL,
    ai_response TEXT NOT NULL,
    sources JSONB DEFAULT '[]'::jsonb,
    screenshot_path TEXT,
    executed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_profiles_status ON browser_profiles(status);
CREATE INDEX idx_profiles_engine_status ON browser_profiles(engine_type, status);
CREATE INDEX idx_proxies_status ON proxies(status);
CREATE INDEX idx_scrape_results_task ON scrape_results(task_id);
CREATE INDEX idx_scrape_results_dedup ON scrape_results(engine_name, input_prompt);
