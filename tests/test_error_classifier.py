"""错误分类器单元测试"""

import unittest
from openai_proxy.utils.error_classifier import ErrorClassifier, ErrorType


class TestErrorClassifier(unittest.TestCase):
    """错误分类器测试"""
    
    def test_classify_by_status_code_429(self):
        """测试 429 状态码分类为配额超出"""
        result = ErrorClassifier.classify_by_status_code(429)
        self.assertEqual(result, ErrorType.QUOTA_EXCEEDED)
    
    def test_classify_by_status_code_401(self):
        """测试 401 状态码分类为认证错误"""
        result = ErrorClassifier.classify_by_status_code(401)
        self.assertEqual(result, ErrorType.AUTH_ERROR)
    
    def test_classify_by_status_code_403(self):
        """测试 403 状态码分类为认证错误"""
        result = ErrorClassifier.classify_by_status_code(403)
        self.assertEqual(result, ErrorType.AUTH_ERROR)
    
    def test_classify_by_status_code_500(self):
        """测试 500 状态码分类为服务器错误"""
        result = ErrorClassifier.classify_by_status_code(500)
        self.assertEqual(result, ErrorType.SERVER_ERROR)
    
    def test_classify_by_status_code_504(self):
        """测试 504 状态码分类为超时错误"""
        result = ErrorClassifier.classify_by_status_code(504)
        self.assertEqual(result, ErrorType.TIMEOUT_ERROR)
    
    def test_classify_by_status_code_unknown(self):
        """测试未知状态码分类为未知错误"""
        result = ErrorClassifier.classify_by_status_code(200)
        self.assertEqual(result, ErrorType.UNKNOWN_ERROR)
    
    def test_classify_by_response_quota_exceeded(self):
        """测试响应内容包含配额错误关键词"""
        response_body = '{"error": {"message": "Rate limit exceeded, quota used up"}}'
        result = ErrorClassifier.classify_by_response(429, response_body)
        self.assertEqual(result, ErrorType.QUOTA_EXCEEDED)
    
    def test_classify_by_response_auth_error(self):
        """测试响应内容包含认证错误关键词"""
        response_body = '{"error": {"message": "Invalid API key provided"}}'
        result = ErrorClassifier.classify_by_response(401, response_body)
        self.assertEqual(result, ErrorType.AUTH_ERROR)
    
    def test_classify_by_response_model_unavailable(self):
        """测试响应内容包含模型不可用关键词"""
        response_body = '{"error": {"message": "Model not found or does not exist"}}'
        result = ErrorClassifier.classify_by_response(404, response_body)
        self.assertEqual(result, ErrorType.MODEL_UNAVAILABLE)
    
    def test_get_handling_strategy_quota_exceeded(self):
        """测试配额超出错误处理策略"""
        strategy = ErrorClassifier.get_handling_strategy(ErrorType.QUOTA_EXCEEDED)
        self.assertEqual(strategy["action"], "disable_model")
        self.assertFalse(strategy["retry"])
    
    def test_get_handling_strategy_auth_error(self):
        """测试认证错误处理策略"""
        strategy = ErrorClassifier.get_handling_strategy(ErrorType.AUTH_ERROR)
        self.assertEqual(strategy["action"], "disable_platform")
        self.assertFalse(strategy["retry"])
        self.assertTrue(strategy["alert"])
    
    def test_get_handling_strategy_network_error(self):
        """测试网络错误处理策略"""
        strategy = ErrorClassifier.get_handling_strategy(ErrorType.NETWORK_ERROR)
        self.assertEqual(strategy["action"], "retry_with_backoff")
        self.assertTrue(strategy["retry"])
        self.assertEqual(strategy["max_retries"], 3)
    
    def test_get_handling_strategy_timeout_error(self):
        """测试超时错误处理策略"""
        strategy = ErrorClassifier.get_handling_strategy(ErrorType.TIMEOUT_ERROR)
        self.assertEqual(strategy["action"], "immediate_failover")
        self.assertFalse(strategy["retry"])
        self.assertEqual(strategy["disable_duration"], 60)


if __name__ == "__main__":
    unittest.main()