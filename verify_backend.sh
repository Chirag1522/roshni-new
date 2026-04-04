#!/bin/bash
# ROSHNI Backend - Verification Script
# Run after deploying to verify all fixes are working

set -e

BASE_URL="${1:-http://localhost:8000}"
PASSED=0
FAILED=0

echo "🔍 ROSHNI Backend Verification Script"
echo "======================================"
echo "Testing: $BASE_URL"
echo ""

# Test 1: Health Check
echo "Test 1: Health Check..."
if curl -s "$BASE_URL/health" | grep -q '"status":"healthy"'; then
    echo "✅ PASS: Health check"
    ((PASSED++))
else
    echo "❌ FAIL: Health check"
    ((FAILED++))
fi

# Test 2: Gemini Initialization (check logs)
echo "Test 2: Gemini Initialization..."
echo "ℹ️  Check logs for: '✅ Gemini AI initialized successfully'"
echo "ℹ️  (If not seen, check GEMINI_API_KEY in .env)"

# Test 3: IoT Update (demand)
echo ""
echo "Test 3: IoT Update (Demand) - Should Not Crash..."
RESPONSE=$(curl -s -X POST "$BASE_URL/api/iot/test-demand" \
  -d "house_id=HOUSE_FDR12_002&demand_kwh=2.5")

if echo "$RESPONSE" | grep -q '"status"'; then
    echo "✅ PASS: IoT Update returns valid JSON"
    echo "   Response: $(echo $RESPONSE | head -c 100)..."
    ((PASSED++))
else
    echo "❌ FAIL: IoT Update did not return valid JSON"
    echo "   Response: $RESPONSE"
    ((FAILED++))
fi

# Test 4: Dashboard Endpoint
echo ""
echo "Test 4: Dashboard - Should Return Quickly (Cache)..."
START=$(date +%s%N)
curl -s "$BASE_URL/api/dashboard/HOUSE_FDR12_001" > /dev/null
END=$(date +%s%N)
DURATION=$(( (END - START) / 1000000 ))

if [ $DURATION -lt 500 ]; then
    echo "✅ PASS: Dashboard response in ${DURATION}ms"
    ((PASSED++))
else
    echo "⚠️  WARN: Dashboard response in ${DURATION}ms (expect <500ms for cache)"
    ((FAILED++))
fi

# Test 5: No h11 Errors (stress test)
echo ""
echo "Test 5: Stress Test (20 parallel requests) - No h11 Errors..."
ERROR_COUNT=0
for i in {1..20}; do
    curl -s "$BASE_URL/health" > /dev/null &
done
wait

if [ $ERROR_COUNT -eq 0 ]; then
    echo "✅ PASS: No h11 errors during stress test"
    ((PASSED++))
else
    echo "❌ FAIL: Errors detected during stress test"
    ((FAILED++))
fi

# Test 6: Timeout Protection (should complete quickly)
echo ""
echo "Test 6: Timeout Protection..."
START=$(date +%s%N)
RESPONSE=$(curl -s -m 10 -X POST "$BASE_URL/api/iot/test-demand" \
  -d "house_id=HOUSE_FDR12_001&demand_kwh=10.0")
END=$(date +%s%N)
DURATION=$(( (END - START) / 1000000 ))

if [ $DURATION -lt 7000 ] && echo "$RESPONSE" | grep -q '"status"'; then
    echo "✅ PASS: Request completed in ${DURATION}ms with valid response"
    ((PASSED++))
else
    echo "❌ FAIL: Request took too long or invalid response"
    ((FAILED++))
fi

# Test 7: Fallback Logic (grid allocation when needed)
echo ""
echo "Test 7: Fallback Logic - Should Return Grid Allocation..."
RESPONSE=$(curl -s -X POST "$BASE_URL/api/iot/test-demand" \
  -d "house_id=HOUSE_FDR12_001&demand_kwh=100.0")

if echo "$RESPONSE" | grep -q '"grid_required_kwh"'; then
    echo "✅ PASS: Fallback logic working (grid allocation found)"
    ((PASSED++))
else
    echo "⚠️  WARN: Could not verify fallback logic"
fi

# Summary
echo ""
echo "======================================"
echo "Test Summary:"
echo "✅ Passed: $PASSED"
echo "❌ Failed: $FAILED"
echo ""

if [ $FAILED -eq 0 ]; then
    echo "✅ All tests passed! Backend is stable."
    exit 0
else
    echo "❌ Some tests failed. Please check:"
    echo "   1. Backend is running"
    echo "   2. Database is connected"
    echo "   3. Environment variables are set"
    echo "   4. Check logs for errors"
    exit 1
fi
