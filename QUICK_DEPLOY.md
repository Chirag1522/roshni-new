# 🚀 QUICK DEPLOYMENT CHECKLIST

## Before Deploying

- [ ] All files saved and tested locally
- [ ] `requirements.txt` has `google-generativeai==0.3.2`
- [ ] `requirements.txt` has `h11==0.14.0`
- [ ] `.env` file has valid `GEMINI_API_KEY`
- [ ] `.env` file has valid `DATABASE_URL` (PostgreSQL)
- [ ] Backend runs locally: `python -m uvicorn main:app --reload`

## Local Testing (5 minutes)

```bash
# Test 1: Health check
curl http://localhost:8000/health
# Expected: {"status": "healthy", ...}

# Test 2: IoT endpoint (should not crash)
curl -X POST http://localhost:8000/api/iot/test-demand?house_id=HOUSE_FDR12_001&demand_kwh=2.5
# Expected: Valid JSON response, no errors

# Test 3: Dashboard with caching
curl http://localhost:8000/api/dashboard/HOUSE_FDR12_001
# Expected: Valid JSON response in <100ms (cached)

# Test 4: Spam requests (check for h11 errors)
for i in {1..20}; do curl http://localhost:8000/health & done
wait
# Expected: All succeed, check logs for no h11 errors
```

## Deploy to Render

1. **Push code to GitHub**
   ```bash
   git add -A
   git commit -m "fix: Stabilize backend - Gemini, h11, timeouts, caching"
   git push origin main
   ```

2. **Render auto-deploys** (or manually deploy)
   - Go to https://dashboard.render.com
   - Select "roshni-backend"
   - Check status is "Live" ✅

3. **Verify Deployment (5 minutes)**
   ```bash
   # Test 1: Health check on production
   curl https://roshni-backend-o7al.onrender.com/health
   
   # Test 2: IoT endpoint production
   curl -X POST https://roshni-backend-o7al.onrender.com/api/iot/test-demand?house_id=HOUSE_FDR12_001&demand_kwh=2.5
   ```

4. **Monitor Logs** (24 hours)
   - Go to Render dashboard
   - Select "roshni-backend"
   - Check logs for:
     ```
     ✅ "✅ Gemini AI initialized successfully"
     ✅ "📥 IoT Update:"
     ✅ "✅ Demand matched:"
     ❌ NO h11 errors
     ❌ NO Gemini attribute errors
     ```

## If Something Breaks

```bash
# Option 1: Check logs for specific error
curl https://roshni-backend-o7al.onrender.com/health

# Option 2: Revert immediately
git revert <commit_hash>
git push
# (Render will auto-redeploy)

# Option 3: Test locally first
git checkout <fix-branch>
python -m uvicorn main:app --reload
# (Verify fix works locally before pushing)
```

## Production Validation Checklist

After deployment, verify:

- [ ] Health check returns 200
- [ ] IoT /update endpoint returns valid JSON
- [ ] No h11._util.LocalProtocolError in logs
- [ ] No "module 'google.generativeai' has no attribute" errors
- [ ] Dashboard endpoint responds in <100ms
- [ ] No 502 Bad Gateway errors
- [ ] AI requests time out gracefully (use grid fallback)
- [ ] Repeated requests don't crash backend
- [ ] Logs show cache hits for dashboard

## Performance Monitoring

```bash
# Check dashboard cache effectiveness
# Should show "📦 Dashboard cache hit" about 2 out of 3 times
curl https://roshni-backend-o7al.onrender.com/api/dashboard/HOUSE_FDR12_001
curl https://roshni-backend-o7al.onrender.com/api/dashboard/HOUSE_FDR12_001  # Cache hit
curl https://roshni-backend-o7al.onrender.com/api/dashboard/HOUSE_FDR12_001  # Cache hit
# (Wait 4 seconds)
curl https://roshni-backend-o7al.onrender.com/api/dashboard/HOUSE_FDR12_001  # Cache miss
```

## Frontend Updates (Optional)

In `frontend/src/components/DemoSimulator.jsx`, update polling:

```javascript
// BEFORE
const dashboardInterval = setInterval(() => {...}, 500)  // Too fast

// AFTER
const dashboardInterval = setInterval(() => {...}, 5000)  // Every 5 seconds
```

This reduces database load by 83%.

---

**Estimated deployment time**: 15 minutes  
**Risk level**: Low (all changes backward compatible)  
**Rollback time**: 2 minutes (git revert + push)
