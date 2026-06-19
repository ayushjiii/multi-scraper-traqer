# src/orchestrator.py
import json
from src.database import DatabaseManager
from src.worker import ExtractionWorker
from src.engine_profiles import ENGINE_PROFILES

class Orchestrator:
    def __init__(self, db_manager: DatabaseManager, engine: str):
        self.db = db_manager
        self.engine = engine.lower()
        if self.engine not in ENGINE_PROFILES:
            raise ValueError(f"Unsupported engine: {self.engine}")
        self.config = ENGINE_PROFILES[self.engine]

    async def check_duplicate(self, input_prompt: str) -> bool:
        count = await self.db.fetchval(
            "SELECT COUNT(*) FROM scrape_results WHERE engine_name = $1 AND input_prompt = $2",
            self.engine, input_prompt
        )
        return count > 0

    async def checkout_profile(self):
        query = """
        UPDATE browser_profiles
        SET status = 'BUSY', last_used_at = CURRENT_TIMESTAMP
        WHERE id = (
            SELECT id FROM browser_profiles
            WHERE status = 'AVAILABLE' AND engine_type = $1
            ORDER BY last_used_at ASC NULLS FIRST
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, profile_name, storage_path, proxy_string;
        """
        profile = await self.db.fetchrow(query, self.engine)
        if profile:
            print(f"[ORCHESTRATOR:{self.engine.upper()}] Checked out profile: {profile['profile_name']}")
            return dict(profile)
        return None

    async def release_profile(self, profile_id: str, success: bool):
        new_status = 'AVAILABLE' if success else 'EXPIRED'
        score_adj  = "+ 1" if success else "- 10"
        await self.db.execute(f"""
            UPDATE browser_profiles
            SET status = $1, trust_score = GREATEST(0, LEAST(100, trust_score {score_adj}))
            WHERE id = $2
        """, new_status, profile_id)
        print(f"[ORCHESTRATOR:{self.engine.upper()}] Released profile {profile_id} → {new_status}")

    async def process_task(self, task_payload: dict):
        task_id = task_payload.get("task_id", "unknown")
        prompt  = task_payload.get("prompt", "")

        if await self.check_duplicate(prompt):
            print(f"[ORCHESTRATOR:{self.engine.upper()}] Duplicate prompt skipped.")
            return True

        print(f"\n[ORCHESTRATOR:{self.engine.upper()}] Processing Task [{task_id}]")

        profile = await self.checkout_profile()
        if not profile:
            print(f"[ORCHESTRATOR:{self.engine.upper()}] No AVAILABLE profile. Requeued.")
            return False

        success = False
        try:
            worker  = ExtractionWorker(
                profile_path=profile["storage_path"],
                engine=self.engine,
                proxy_string=profile.get("proxy_string")
            )
            results = await worker.execute_task(
                prompt=prompt,
                task_id=task_id
            )

            await self.db.execute("""
                INSERT INTO scrape_results
                    (profile_id, task_id, engine_name, input_prompt, ai_response, sources, screenshot_path)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            """,
                profile["id"],
                task_id,
                self.engine,
                prompt,
                results["ai_response"],
                json.dumps(results.get("sources", [])),
                results["screenshot_path"]
            )

            print(f"[ORCHESTRATOR:{self.engine.upper()}] Task {task_id} saved successfully.")
            success = True

        except Exception as e:
            error_msg = str(e)
            print(f"[ORCHESTRATOR:{self.engine.upper()}] Task {task_id} failed: {error_msg}")
            
            # Smart Engine-Specific Proxy Banning
            if any(term in error_msg for term in ["Proxy IP burned", "Cloudflare", "Verification wall", "Timeout"]):
                if profile.get("proxy_string"):
                    try:
                        banned_col = f"{self.engine}_banned"
                        await self.db.execute(f"""
                            UPDATE proxies SET {banned_col} = TRUE WHERE connection_string = $1
                        """, profile["proxy_string"])
                        print(f"[ORCHESTRATOR:{self.engine.upper()}] Proxy flagged as banned for this engine.")
                    except Exception:
                        pass

        finally:
            if profile:
                await self.release_profile(profile["id"], success)

        return success