"""
AI-powered allocation strategy service.
Uses Gemini API with proper error handling, timeouts, and fallback logic.
✅ Production-ready with comprehensive error handling
"""
import asyncio
import logging
from typing import Dict, Optional
from config import settings

logger = logging.getLogger(__name__)


class AIPricingService:
    """Uses Gemini API to provide AI-driven pricing and allocation logic with fallbacks."""
    
    def __init__(self):
        """Initialize Gemini AI client safely."""
        self.client = None
        self.available = False
        
        if settings.gemini_api_key:
            try:
                import google.generativeai as genai
                genai.configure(api_key=settings.gemini_api_key)
                self.client = genai.GenerativeModel("gemini-pro")
                self.available = True
                logger.info("✅ Gemini AI initialized successfully")
            except ImportError:
                logger.warning("⚠️ google-generativeai not installed. Using fallback logic.")
            except Exception as e:
                logger.warning(f"⚠️ Gemini initialization failed: {str(e)}. Using fallback logic.")
        else:
            logger.warning("⚠️ GEMINI_API_KEY not configured. Using fallback logic.")

    async def get_allocation_strategy(
        self,
        available_pool_kwh: float,
        demand_kwh: float,
        grid_rate_inr: float,
        pool_rate_inr: float,
        house_priority: int = 5,
    ) -> Dict:
        """
        Get AI-driven allocation strategy with timeout protection.
        If AI fails or times out, automatically falls back to rule-based logic.
        
        Args:
            available_pool_kwh: Solar generation available in the pool
            demand_kwh: Consumer demand
            grid_rate_inr: Grid electricity rate in INR/kWh
            pool_rate_inr: Pool electricity rate in INR/kWh
            house_priority: Priority level (1-10, higher = more priority)
            
        Returns:
            dict with keys: pool_kwh, grid_kwh, reasoning, ai_used
        """
        if not self.available or not self.client:
            return self._fallback_allocation(
                available_pool_kwh, demand_kwh, grid_rate_inr, pool_rate_inr, house_priority
            )
        
        try:
            # Run AI allocation with 5-second timeout
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._ai_allocation_sync,
                    available_pool_kwh,
                    demand_kwh,
                    grid_rate_inr,
                    pool_rate_inr,
                    house_priority,
                ),
                timeout=5.0,
            )
            logger.info(f"✅ AI allocation successful: Pool={result['pool_kwh']:.2f}kWh")
            return result
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ Gemini API timeout (5s). Using fallback logic.")
            return self._fallback_allocation(
                available_pool_kwh, demand_kwh, grid_rate_inr, pool_rate_inr, house_priority
            )
        except Exception as e:
            logger.error(f"❌ AI allocation error: {str(e)}. Using fallback logic.")
            return self._fallback_allocation(
                available_pool_kwh, demand_kwh, grid_rate_inr, pool_rate_inr, house_priority
            )

    def _ai_allocation_sync(
        self,
        available_pool_kwh: float,
        demand_kwh: float,
        grid_rate_inr: float,
        pool_rate_inr: float,
        house_priority: int,
    ) -> Dict:
        """Synchronous AI allocation (runs in thread to avoid blocking)."""
        if not self.client:
            return self._fallback_allocation(
                available_pool_kwh, demand_kwh, grid_rate_inr, pool_rate_inr, house_priority
            )
        
        prompt = f"""You are a solar energy allocation optimizer for a virtual net metering system.

Context:
- Available solar from pool: {available_pool_kwh:.2f} kWh
- Consumer demand: {demand_kwh:.2f} kWh
- Pool rate: ₹{pool_rate_inr:.2f}/kWh
- Grid rate: ₹{grid_rate_inr:.2f}/kWh
- Consumer priority (1-10): {house_priority}

Analyze and recommend allocation. Your response MUST ONLY be valid JSON like:
{{"pool_allocation_kwh": <number>, "reasoning": "<short reason>"}}

Consider: fairness, cost efficiency, grid stability, and consumer priority.
Maximum 100 words in reasoning."""
        
        try:
            response = self.client.generate_content(prompt, request_options={"timeout": 5})
            
            # Parse response text directly
            response_text = response.text.strip()
            
            # Try to extract JSON from response
            import json
            try:
                # If response is pure JSON
                data = json.loads(response_text)
                pool_kwh = float(data.get("pool_allocation_kwh", 0))
            except json.JSONDecodeError:
                # If response contains JSON within text
                import re
                match = re.search(r'\{[^}]*"pool_allocation_kwh"[^}]*\}', response_text)
                if match:
                    data = json.loads(match.group())
                    pool_kwh = float(data.get("pool_allocation_kwh", 0))
                else:
                    # Fallback: allocate proportionally
                    pool_kwh = min(available_pool_kwh, demand_kwh)
                    response_text = "Failed to parse AI response, using proportional allocation."
            
            # Ensure bounds
            pool_kwh = min(pool_kwh, available_pool_kwh, demand_kwh)
            pool_kwh = max(0, pool_kwh)
            
            return {
                "pool_kwh": pool_kwh,
                "grid_kwh": max(0, demand_kwh - pool_kwh),
                "reasoning": response_text[:200],
                "ai_used": True,
            }
        except Exception as e:
            logger.error(f"AI allocation sync error: {str(e)}")
            return self._fallback_allocation(
                available_pool_kwh, demand_kwh, grid_rate_inr, pool_rate_inr, house_priority
            )

    def _fallback_allocation(
        self,
        available_pool_kwh: float,
        demand_kwh: float,
        grid_rate_inr: float,
        pool_rate_inr: float,
        house_priority: int,
    ) -> Dict:
        """Rule-based allocation when AI is unavailable or fails."""
        
        # Priority-based multiplier (1-10 scale)
        # Priority 5 = 1.0x, Priority 10 = 1.25x, Priority 1 = 0.75x
        priority_multiplier = 0.75 + (house_priority - 1) * 0.05
        
        # Calculate maximum pool allocation based on availability and priority
        max_pool = available_pool_kwh * priority_multiplier
        
        # Actual allocation is minimum of max_pool and demand
        pool_allocation = min(max_pool, demand_kwh)
        pool_allocation = max(0, pool_allocation)
        
        # Rest comes from grid
        grid_allocation = max(0, demand_kwh - pool_allocation)
        
        reasoning = (
            f"Priority-based fallback (priority={house_priority}): "
            f"Allocated {pool_allocation:.2f}kWh from pool, "
            f"{grid_allocation:.2f}kWh from grid fallback."
        )
        
        return {
            "pool_kwh": pool_allocation,
            "grid_kwh": grid_allocation,
            "reasoning": reasoning,
            "ai_used": False,
        }

    async def calculate_dynamic_pricing(self, pool_utilization_percent: float) -> Dict:
        """
        Adjust pricing based on pool utilization (non-blocking).
        """
        base_pool_rate = settings.solar_pool_rate
        
        # Higher utilization = slightly higher price
        if pool_utilization_percent > 80:
            adjusted_rate = base_pool_rate * 1.1
        elif pool_utilization_percent > 60:
            adjusted_rate = base_pool_rate * 1.05
        else:
            adjusted_rate = base_pool_rate
        
        return {
            "base_rate_inr": base_pool_rate,
            "adjusted_rate_inr": adjusted_rate,
            "utilization_percent": pool_utilization_percent,
        }


# Global singleton instance
ai_service = AIPricingService()
