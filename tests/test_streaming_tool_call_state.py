import json
from openai_proxy.adapter.responses import ResponsesAdapter


def test_streaming_tool_call_state_management():
    """测试流式工具调用状态管理功能"""
    adapter = ResponsesAdapter()
    
    # 模拟第一个chunk（包含完整信息）
    first_chunk_data = {
        "choices": [{
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": "call-123",
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "arguments": ""
                    }
                }]
            }
        }]
    }
    
    first_chunk_event = f"data: {json.dumps(first_chunk_data)}\n\n"
    result = adapter.convert_stream_event(first_chunk_event)
    
    # 验证第一个chunk正确建立了状态缓存
    assert result is not None
    assert "response.function_call_arguments.delta" in result
    
    # 解析返回的事件
    event_lines = result.strip().split('\n')
    event_type_line = event_lines[0]
    data_line = event_lines[1]
    
    assert event_type_line == "event: response.function_call_arguments.delta"
    
    event_data = json.loads(data_line[6:])  # 去掉 "data: " 前缀
    assert event_data["call_id"] == "call-123"
    assert event_data["name"] == "exec_command"
    assert event_data["arguments"] == ""
    
    print("✓ 第一个chunk正确建立状态缓存")
    
    # 模拟后续chunks（只包含增量arguments）
    subsequent_chunks = [
        {"function": {"arguments": "{"}},
        {"function": {"arguments": "\n"}},
        {"function": {"arguments": " \"cmd\": \"ls\""}},
        {"function": {"arguments": "\n"}}
    ]
    
    for i, chunk_data in enumerate(subsequent_chunks):
        chunk_event_data = {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": chunk_data["function"]
                    }]
                }
            }]
        }
        
        chunk_event = f"data: {json.dumps(chunk_event_data)}\n\n"
        result = adapter.convert_stream_event(chunk_event)
        
        # 验证后续chunks能正确使用缓存的call_id和name
        assert result is not None
        assert "response.function_call_arguments.delta" in result
        
        event_lines = result.strip().split('\n')
        data_line = event_lines[1]
        event_data = json.loads(data_line[6:])
        
        assert event_data["call_id"] == "call-123", f"Chunk {i+1}: call_id should be from cache"
        assert event_data["name"] == "exec_command", f"Chunk {i+1}: name should be from cache"
        assert event_data["arguments"] == chunk_data["function"]["arguments"]
        
        print(f"✓ 后续chunk {i+1} 正确使用缓存状态")
    
    # 模拟[DONE]事件，验证状态清理
    done_event = "data: [DONE]\n\n"
    result = adapter.convert_stream_event(done_event)
    
    assert result is not None
    assert "response.completed" in result
    
    # 验证状态已被清理
    assert len(adapter._streaming_tool_call_state) == 0
    print("✓ [DONE]事件后状态正确清理")
    
    print("\n🎉 所有流式工具调用状态管理测试通过！")


def test_multiple_parallel_tool_calls():
    """测试多个并行工具调用的状态管理"""
    adapter = ResponsesAdapter()
    
    # 模拟两个并行的工具调用
    parallel_chunks = [
        {
            "choices": [{
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "tool_a",
                                "arguments": ""
                            }
                        },
                        {
                            "index": 1,
                            "id": "call-2",
                            "type": "function",
                            "function": {
                                "name": "tool_b",
                                "arguments": ""
                            }
                        }
                    ]
                }
            }]
        }
    ]
    
    # 处理第一个包含两个工具调用的chunk
    for chunk_data in parallel_chunks:
        chunk_event = f"data: {json.dumps(chunk_data)}\n\n"
        result = adapter.convert_stream_event(chunk_event)
        
        # 验证两个工具调用的状态都被正确缓存
        assert 0 in adapter._streaming_tool_call_state
        assert 1 in adapter._streaming_tool_call_state
        assert adapter._streaming_tool_call_state[0]["call_id"] == "call-1"
        assert adapter._streaming_tool_call_state[0]["name"] == "tool_a"
        assert adapter._streaming_tool_call_state[1]["call_id"] == "call-2"
        assert adapter._streaming_tool_call_state[1]["name"] == "tool_b"
        
        print("✓ 多个并行工具调用状态正确缓存")
    
    # 模拟后续的增量chunks
    incremental_chunks = [
        {"index": 0, "function": {"arguments": "arg1"}},
        {"index": 1, "function": {"arguments": "arg2"}}
    ]
    
    for chunk_info in incremental_chunks:
        chunk_data = {
            "choices": [{
                "delta": {
                    "tool_calls": [chunk_info]
                }
            }]
        }
        
        chunk_event = f"data: {json.dumps(chunk_data)}\n\n"
        result = adapter.convert_stream_event(chunk_event)
        
        assert result is not None
        event_lines = result.strip().split('\n')
        data_line = event_lines[1]
        event_data = json.loads(data_line[6:])
        
        # 验证每个工具调用都能从缓存中获取正确的元数据
        if chunk_info["index"] == 0:
            assert event_data["call_id"] == "call-1"
            assert event_data["name"] == "tool_a"
        else:
            assert event_data["call_id"] == "call-2"
            assert event_data["name"] == "tool_b"
            
        print(f"✓ 工具调用 index={chunk_info['index']} 正确使用缓存状态")
    
    print("\n🎉 多工具调用并行测试通过！")


if __name__ == "__main__":
    print("=== 测试流式工具调用状态管理 ===\n")
    test_streaming_tool_call_state_management()
    print("\n=== 测试多工具调用并行处理 ===\n")
    test_multiple_parallel_tool_calls()