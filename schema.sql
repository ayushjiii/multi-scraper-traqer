CREATE TYPE profile_status AS ENUM ('AVAILABLE', 'BUSY', 'COOLDOWN', 'EXPIRED');
CREATE TYPE proxy_status AS ENUM ('ACTIVE', 'DOWN', 'RATE_LIMITED');

CREATE TABLE browser_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_name VARCHAR(50) UNIQUE NOT NULL,
    engine_type VARCHAR(30) NOT NULL,
    storage_path TEXT NOT NULL,
    proxy_string TEXT,
    status profile_status DEFAULT 'AVAILABLE',
    trust_score INT DEFAULT 0,
    last_used_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE proxies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_string TEXT UNIQUE NOT NULL,
    status proxy_status DEFAULT 'ACTIVE',
    consecutive_failures INT DEFAULT 0,
    last_tested_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
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