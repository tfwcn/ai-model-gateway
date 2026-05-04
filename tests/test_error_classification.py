#!/usr/bin/env python3
"""
错误分类系统测试脚本
"""
import asyncio
import sys
from openai_proxy.model.error_classifier import ErrorClassifier, ErrorCategory, ClassifiedError


def test_http_error_classification():
    """测试HTTP错误分类"""
    print("\n" + "="*60)
    print("测试1: HTTP错误分类")
    print("="*60)
    
    test_cases = [
        (400, "Bad Request", "http_400_bad_request"),
        (401, "Invalid API Key", "http_401_unauthorized"),
        (403, "Forbidden", "http_403_forbidden"),
        (404, "Model not found", "http_404_not_found"),
        (429, "Rate limit exceeded", "http_429_rate_limit"),
        (500, "Internal Server Error", "http_500_internal_error"),
        (502, "Bad Gateway", "http_502_bad_gateway"),
        (503, "Service Unavailable", "http_503_service_unavailable"),
        (504, "Gateway Timeout", "http_504_gateway_timeout"),
    ]
    
    for status_code, error_text, expected_category in test_cases:
        classified = ErrorClassifier.classify_http_error(
            status_code, error_text, "test-model"
        )
        
        assert classified.category.value == expected_category, \
            f"Expected {expected_category}, got {classified.category.value}"
        
        summary = ErrorClassifier.get_error_summary(classified)
        print(f"\n✓ HTTP {status_code}: {summary}")
        print(f"  - 可重试: {'是' if classified.is_retryable else '否'}")
        print(f"  - 禁用模型: {'是' if classified.should_disable_model else '否'}")
        print(f"  - 重试延迟: {classified.retry_delay_seconds}秒")
    
    print("\n✅ HTTP错误分类测试通过")


def test_timeout_error_classification():
    """测试超时错误分类"""
    print("\n" + "="*60)
    print("测试2: 超时错误分类")
    print("="*60)
    
    classified = ErrorClassifier.classify_timeout_error(
        "test-model", 60.5, 60
    )
    
    assert classified.category == ErrorCategory.NETWORK_TIMEOUT
    assert classified.is_retryable == True
    assert classified.should_disable_model == False
    
    summary = ErrorClassifier.get_error_summary(classified)
    print(f"\n✓ 超时错误: {summary}")
    print(f"  - 消息: {classified.message}")
    print(f"  - 可重试: {'是' if classified.is_retryable else '否'}")
    print(f"  - 禁用模型: {'是' if classified.should_disable_model else '否'}")
    
    print("\n✅ 超时错误分类测试通过")


def test_connection_error_classification():
    """测试连接错误分类"""
    print("\n" + "="*60)
    print("测试3: 连接错误分类")
    print("="*60)
    
    # 测试DNS错误
    dns_error = Exception("DNS resolution failed")
    classified_dns = ErrorClassifier.classify_connection_error(dns_error, "test-model")
    print(f"\n✓ DNS错误: {ErrorClassifier.get_error_summary(classified_dns)}")
    print(f"  - 分类: {classified_dns.category.value}")
    
    # 测试连接拒绝
    conn_refused = Exception("Connection refused")
    classified_conn = ErrorClassifier.classify_connection_error(conn_refused, "test-model")
    print(f"\n✓ 连接拒绝: {ErrorClassifier.get_error_summary(classified_conn)}")
    print(f"  - 分类: {classified_conn.category.value}")
    
    print("\n✅ 连接错误分类测试通过")


def test_invalid_response_classification():
    """测试无效响应分类"""
    print("\n" + "="*60)
    print("测试4: 无效响应分类")
    print("="*60)
    
    # 测试缺少content
    classified_missing = ErrorClassifier.classify_invalid_response(
        "test-model", "Response missing content field"
    )
    assert classified_missing.category == ErrorCategory.MISSING_CONTENT
    print(f"\n✓ 缺少content: {ErrorClassifier.get_error_summary(classified_missing)}")
    
    # 测试无效格式
    classified_invalid = ErrorClassifier.classify_invalid_response(
        "test-model", "Invalid response format"
    )
    assert classified_invalid.category == ErrorCategory.INVALID_RESPONSE_FORMAT
    print(f"✓ 无效格式: {ErrorClassifier.get_error_summary(classified_invalid)}")
    
    print("\n✅ 无效响应分类测试通过")


def test_unknown_error_classification():
    """测试未知错误分类"""
    print("\n" + "="*60)
    print("测试5: 未知错误分类")
    print("="*60)
    
    unknown_error = Exception("Some unexpected error occurred")
    classified = ErrorClassifier.classify_unknown_error(
        unknown_error, "test-model", 5.2
    )
    
    assert classified.category == ErrorCategory.UNKNOWN_ERROR
    assert classified.is_retryable == True
    assert classified.should_disable_model == False
    
    summary = ErrorClassifier.get_error_summary(classified)
    print(f"\n✓ 未知错误: {summary}")
    print(f"  - 异常类型: {classified.metadata['exception_type']}")
    print(f"  - 耗时: {classified.metadata['elapsed_time']}秒")
    
    print("\n✅ 未知错误分类测试通过")


def test_error_strategies():
    """测试错误处理策略"""
    print("\n" + "="*60)
    print("测试6: 错误处理策略验证")
    print("="*60)
    
    strategies_to_test = [
        (ErrorCategory.NETWORK_TIMEOUT, True, False),
        (ErrorCategory.HTTP_401_UNAUTHORIZED, False, True),
        (ErrorCategory.HTTP_429_RATE_LIMIT, True, True),
        (ErrorCategory.HTTP_500_INTERNAL_ERROR, True, False),
        (ErrorCategory.MISSING_CONTENT, False, True),
    ]
    
    for category, expected_retryable, expected_disable in strategies_to_test:
        strategy = ErrorClassifier.ERROR_STRATEGIES[category]
        assert strategy["is_retryable"] == expected_retryable, \
            f"{category.value}: expected retryable={expected_retryable}"
        assert strategy["should_disable_model"] == expected_disable, \
            f"{category.value}: expected disable={expected_disable}"
        print(f"✓ {category.value}: 可重试={expected_retryable}, 禁用={expected_disable}")
    
    print("\n✅ 错误处理策略测试通过")


def main():
    """运行所有测试"""
    print("\n" + "🧪 开始测试错误分类系统")
    
    try:
        test_http_error_classification()
        test_timeout_error_classification()
        test_connection_error_classification()
        test_invalid_response_classification()
        test_unknown_error_classification()
        test_error_strategies()
        
        print("\n" + "="*60)
        print("🎉 所有测试通过！")
        print("="*60)
        print("\n错误分类系统工作正常，可以投入使用。")
        return 0
        
    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ 测试出错: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
