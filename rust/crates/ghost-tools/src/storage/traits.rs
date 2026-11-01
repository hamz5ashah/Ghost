//! MemoryBackend trait for all storage backends.

use ghost_core::{GhostError, RetrievalResult};
use serde_json::Value;

pub trait MemoryBackend: Send + Sync {
    fn backend_id(&self) -> &str;
    fn store(
        &self,
        content: &str,
        source: &str,
        metadata: Option<&Value>,
    ) -> Result<String, GhostError>;
    fn retrieve(
        &self,
        query: &str,
        top_k: usize,
    ) -> Result<Vec<RetrievalResult>, GhostError>;
    fn delete(&self, doc_id: &str) -> Result<bool, GhostError>;
    fn clear(&self) -> Result<(), GhostError>;
    fn count(&self) -> Result<usize, GhostError>;
}
