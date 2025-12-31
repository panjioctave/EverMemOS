"""
Rerank Service - Hybrid Implementation with Automatic Fallback

This is the main reranking service with built-in resilience.
Implements a hybrid strategy: custom self-deployed service (primary) 
with automatic fallback to DeepInfra (secondary).

Usage:
    from agentic_layer.rerank_service import get_rerank_service
    
    service = get_rerank_service()
    result = await service.rerank_memories(query, retrieve_response)
"""

import logging
import os
from typing import Optional, Any, Union, List, Dict
from dataclasses import dataclass, field

from core.di import service

from agentic_layer.rerank_interface import (
    RerankServiceInterface,
    RerankError,
    RerankMemResponse,
)
from agentic_layer.rerank_custom import CustomRerankService, CustomRerankConfig
from agentic_layer.rerank_deepinfra import (
    DeepInfraRerankService,
    DeepInfraRerankConfig,
)

logger = logging.getLogger(__name__)


@dataclass
class HybridRerankConfig:
    """Configuration for hybrid rerank service with fallback"""

    # Custom service config
    custom_config: CustomRerankConfig = field(default_factory=CustomRerankConfig)

    # DeepInfra config
    deepinfra_config: DeepInfraRerankConfig = field(
        default_factory=DeepInfraRerankConfig
    )

    # Fallback behavior
    enable_fallback: bool = True
    max_custom_failures: int = 3

    # Runtime state (failure tracking)
    _custom_failure_count: int = field(default=0, init=False, repr=False)

    def __post_init__(self):
        """Load hybrid service configuration from environment"""
        self.enable_fallback = (
            os.getenv("ENABLE_RERANK_FALLBACK", "true").lower() == "true"
        )
        self.max_custom_failures = int(
            os.getenv("MAX_CUSTOM_RERANK_FAILURES", str(self.max_custom_failures))
        )


class HybridRerankService(RerankServiceInterface):
    """
    Hybrid Reranking Service with Automatic Fallback
    
    This service implements a dual-strategy approach:
    1. Implements RerankServiceInterface with full API
    2. Primary: Custom self-deployed service (low cost, fast)
    3. Secondary: DeepInfra commercial API (high availability)
    4. Automatic failover on errors with failure tracking
    5. All method calls transparently use fallback logic
    
    Strategy Benefits:
    - Cost optimization: ~95% savings with custom service
    - High availability: Automatic failover ensures reliability
    - Zero downtime: Continues working during custom service maintenance
    
    Usage:
        service = HybridRerankService()
        result = await service.rerank_memories(query, response)  # Auto-fallback built-in
    """

    def __init__(self, config: Optional[HybridRerankConfig] = None):
        if config is None:
            config = HybridRerankConfig()

        self.config = config
        
        # Initialize both services
        self.custom_service = CustomRerankService(config.custom_config)
        self.deepinfra_service = DeepInfraRerankService(config.deepinfra_config)
        
        # Current active service
        self._current_service: RerankServiceInterface = self.custom_service

        logger.info(
            f"Initialized HybridRerankService | "
            f"fallback_enabled={config.enable_fallback} | "
            f"max_failures={config.max_custom_failures}"
        )

    def get_service(self) -> RerankServiceInterface:
        """
        Get the current active service (for advanced usage)
        
        Returns:
            RerankServiceInterface: The active service (custom or deepinfra)
            
        Note: Prefer using hybrid service methods directly for automatic fallback
        """
        return self._current_service

    async def rerank_memories(
        self, query: str, retrieve_response: Any, instruction: str = None
    ) -> Union[RerankMemResponse, List[Dict[str, Any]]]:
        """Rerank memories with automatic fallback"""
        return await self.execute_with_fallback(
            "rerank_memories",
            lambda: self.custom_service.rerank_memories(query, retrieve_response, instruction),
            lambda: self.deepinfra_service.rerank_memories(query, retrieve_response, instruction),
        )

    async def execute_with_fallback(self, operation_name: str, primary_func, fallback_func):
        """
        Execute operation with automatic fallback logic
        
        Args:
            operation_name: Name of the operation for logging
            primary_func: Function to call on primary service
            fallback_func: Function to call on fallback service
            
        Returns:
            Result from primary or fallback service
            
        Raises:
            RerankError: If both services fail
        """
        # Try primary (custom) service first
        try:
            result = await primary_func()
            # Reset failure count on success
            self.config._custom_failure_count = 0
            return result

        except Exception as primary_error:
            # Increment failure count
            self.config._custom_failure_count += 1

            logger.warning(
                f"Custom rerank service {operation_name} failed "
                f"(count: {self.config._custom_failure_count}): {primary_error}"
            )

            # Check if fallback is enabled
            if not self.config.enable_fallback:
                logger.error("Fallback disabled, re-raising error")
                raise RerankError(
                    f"Custom service failed and fallback is disabled: {primary_error}"
                )

            # Check if exceeded max failures
            if self.config._custom_failure_count >= self.config.max_custom_failures:
                logger.warning(
                    f"⚠️ Custom rerank service exceeded max failures ({self.config.max_custom_failures}), "
                    f"using DeepInfra fallback"
                )

            # Try fallback service
            try:
                logger.info(f"🔄 Falling back to DeepInfra for {operation_name}")
                result = await fallback_func()
                return result

            except Exception as fallback_error:
                logger.error(f"❌ Fallback also failed: {fallback_error}")
                raise RerankError(
                    f"Both custom and fallback services failed. "
                    f"Custom: {primary_error}, Fallback: {fallback_error}"
                )

    def get_failure_count(self) -> int:
        """Get current custom service failure count"""
        return self.config._custom_failure_count

    def reset_failure_count(self):
        """Reset failure count (useful for health check recovery)"""
        self.config._custom_failure_count = 0
        logger.info("Reset custom rerank service failure count to 0")

    async def close(self):
        """Close all services"""
        await self.custom_service.close()
        await self.deepinfra_service.close()


# Global service instance (lazy initialization)
_service_instance: Optional[HybridRerankService] = None


def get_hybrid_service() -> HybridRerankService:
    """
    Get the global hybrid service instance (singleton)
    
    Returns:
        HybridRerankService: The global hybrid service instance
    """
    global _service_instance
    if _service_instance is None:
        _service_instance = HybridRerankService()
    return _service_instance


# Main entry point - registered with DI container
@service(name="rerank_service", primary=True)
def get_rerank_service() -> RerankServiceInterface:
    """
    Get the reranking service (main entry point)
    
    Returns the hybrid service which implements RerankServiceInterface.
    All method calls automatically go through fallback logic.
    
    Returns:
        RerankServiceInterface: The hybrid service with automatic fallback
        
    Example:
        ```python
        from agentic_layer.rerank_service import get_rerank_service
        
        service = get_rerank_service()  # Returns hybrid service with fallback
        result = await service.rerank_memories(query, retrieve_response)  # Auto-fallback
        await service.close()
        ```
    """
    return get_hybrid_service()  # Return hybrid service (implements RerankServiceInterface)


# Export public API
__all__ = [
    "get_rerank_service",
]
