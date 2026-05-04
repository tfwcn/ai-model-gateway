import asyncio
# import pytest  # Commented out for manual execution without pip install
from openai_proxy.adapter.responses import ResponsesAdapter
from openai_proxy.utils.session import RedisSessionStore


class MockRedisSessionStore(RedisSessionStore):
    """用于测试的模拟 Redis 存储"""
    def __init__(self):
        self.store = {}

    async def get_history(self, response_id: str):
        return self.store.get(response_id, [])

    async def save_session(self, response_id: str, messages: list, ttl: int = None):
        self.store[response_id] = messages


# @pytest.mark.asyncio
async def test_convert_request_basic():
    adapter = ResponsesAdapter(session_store=MockRedisSessionStore())
    
    payload = {
        "model": "gpt-4",
        "input": [
            {"type": "message", "role": "user", "content": "Hello"},
            {"type": "message", "role": "assistant", "content": "Hi there!"},
            {"type": "message", "role": "user", "content": "How are you?"}
        ],
        "instructions": "You are a helpful assistant.",
        "max_output_tokens": 100
    }
    
    result = await adapter.convert_request(payload)
    
    assert result["model"] == "gpt-4"
    assert len(result["messages"]) == 4 # system + 3 messages
    assert result["messages"][0]["role"] == "system"
    assert result["max_tokens"] == 100


# @pytest.mark.asyncio
async def test_convert_with_history():
    store = MockRedisSessionStore()
    adapter = ResponsesAdapter(session_store=store)
    
    # 先保存一段历史
    history_id = "prev_123"
    await store.save_session(history_id, [{"role": "user", "content": "First message"}])
    
    payload = {
        "model": "gpt-4",
        "input": [{"type": "message", "role": "user", "content": "Second message"}],
        "previous_response_id": history_id
    }
    
    result = await adapter.convert_request(payload)
    
    assert len(result["messages"]) == 2 # 1 history + 1 current
    assert result["messages"][0]["content"] == "First message"
    assert result["messages"][1]["content"] == "Second message"


def test_build_response_object():
    adapter = ResponsesAdapter()
    
    chat_resp = {
        "id": "chatcmpl-123",
        "created": 1677652288,
        "model": "gpt-4",
        "choices": [{"message": {"content": "This is a test."}}],
        "usage": {"total_tokens": 10}
    }
    
    original_req = {"model": "gpt-4"}
    
    resp_obj, new_id = adapter.build_response_object(chat_resp, original_req)
    
    assert resp_obj["object"] == "response"
    assert resp_obj["status"] == "completed"
    assert resp_obj["output"][0]["content"][0]["text"] == "This is a test."
    assert new_id.startswith("resp_")


if __name__ == "__main__":
    asyncio.run(test_convert_request_basic())
    asyncio.run(test_convert_with_history())
    test_build_response_object()
    print("All tests passed!")
