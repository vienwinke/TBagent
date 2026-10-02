# treatbord 侧接入要点（Spring Boot 2.7 / 3.x 通用）

> 目标：让小程序能"流式"看到答案，同时把**身份**安全地传给 AI 边车。
> 三个必须守住的点：① 只信服务端解析出的 user_id；② 用内部 JWT 而不是自定义头；③ 流式通道选 WSS。

## 1. 小程序侧：流式通道

⚠️ **`wx.request` 不支持标准 SSE 流式消费**（拿不到增量，只能等整段返回）。两条可行路线：

| 方案 | 写法 | 评价 |
|---|---|---|
| **WSS（推荐）** | `wx.connectSocket({url:"wss://…/ai/ws"})`，服务端推 JSON 帧 | 原生支持、可中断、可重连、不受基础库版本限制 |
| `enableChunked`（降级） | `wx.request({enableChunked:true, responseType:"arraybuffer"})` + `onChunkReceived` 里自行按 `\n\n` 切分 SSE 分片 | 需要基础库 ≥ 2.20.2，且要自己写分片解析 |

小程序端要点：
- 两个域名都要在**微信后台配置白名单**（`request` 合法域名 / `socket` 合法域名），必须 HTTPS/WSS 且已备案。
- 首字延迟目标 ≤ 1.2s：先把 `meta` 帧推给前端（显示"正在分析…"），再推 `delta`。
- 断线重连后带 `client_msg_id` 重发，服务端幂等（Redis `SETNX`）不会重复计费。

## 2. treatbord 侧：签发内部 JWT

```java
// 依赖：io.jsonwebtoken:jjwt-api/impl/jackson（2.7 与 3.x 通用）
String issueAiToken(long userId, String role, String sessionJti) {   // ★ sessionJti 见下方红线
    return Jwts.builder()
        .setSubject(String.valueOf(userId))          // ← sub = user_id，边车只认这个
        .claim("role", role)                          // USER / ADMIN
        .setId(UUID.randomUUID().toString())          // jti：便于审计与吊销
        .setAudience("ai-sidecar")
        .setIssuedAt(new Date())
        .setExpiration(new Date(System.currentTimeMillis() + 5 * 60_000))  // ≤5min
        .signWith(secretKey, SignatureAlgorithm.HS256) // 上线建议换 RS256（边车只持公钥）
        .compact();
}
```

**红线 ①**：`userId` **只能**来自服务端校验过的登录态（openid → user 表）。
请求体里**不要**接受 `user_id` 字段——一旦接受，行级隔离就形同虚设。

**红线 ②**：内部 JWT 的 `jti` **必须沿用调用方当前会话的 jti**（`UserContext.get().getJti()`），
不要每次新生成一个。原因：treatbord 的 `TokenBlacklistService` 在登出/封禁/注销时写的是
`token:blacklist:<会话jti>`，边车查的是同一个键 —— **只有 jti 一致，"踢人下线"才会对 AI 问答即时生效**；
每次新生成 jti 的话，登出后边车仍能用旧 token 继续查（最长 5 分钟窗口）。
（已实测：Java 签发 → 边车 200 → 写黑名单 → 同一 token 401 → 移出黑名单 → 200。）

## 实测踩到的三个坑（L3 端到端验证，2026-10-02）

1. **JDK HttpClient 会对明文端口尝试 h2c 升级**（发 `Upgrade: h2c` + `HTTP2-Settings` +
   `Transfer-encoding: chunked`）。边车是 uvicorn，不支持该升级 → **请求体被丢掉**，
   边车报 `422 {'loc':['body'],'msg':'Field required'}`；而用 curl/Postman 测同一接口完全正常，
   极难定位（最后靠抓包才看到）。修法：`HttpClient.newBuilder().version(HTTP_1_1)`。
2. **键名必须 snake_case**：请求体走 L2 契约（`session_id` / `client_msg_id` / `message_id`），
   Java record 上要加 `@JsonProperty`，否则序列化成 camelCase → 边车 **422**。
4. **边车的 4xx 语义要透传**：`proxy` 里不要把一切非 2xx 都折成 502 ——
   "这条会话/回答不是你的"（404）折成 502 后，前端看到的是"服务异常"，排查方向就全错了。
3. **`RestClient` 的 `spec.body(x)` 返回的是新对象**，不接住等于没挂 body（同样表现为 422 且 body 为空）。
   另外错误信息要带上响应体，否则只有状态码，排查只能靠猜。

## 已生成的骨架（`com.treatbord.module.ai`，编译与单测均已通过；**未提交，等你 review**）：
`AiChatController`（SSE 代理）· `AiSidecarClient`（逐行转发 + trace 贯穿）· `AiTokenService`（内部 JWT）·
`AiChatRequest`（**无 user_id 字段**）· `AiSidecarConfig`（转发线程池）· `AiTokenServiceTest`（6 条）。
配置键（有默认值，可只加环境变量）：`treatbord.ai.sidecar.base-url / jwt-secret / timeout-ms / token-ttl-seconds`。

## 3. treatbord 侧：SSE 代理（Spring MVC，2.7 / 3.x 通用）

```java
@PostMapping(value = "/ai/chat", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
public SseEmitter chat(@RequestBody ChatReq req, HttpServletRequest http) {
    long userId = CurrentUser.id(http);              // 服务端会话解析，绝不用 req.userId
    SseEmitter emitter = new SseEmitter(8_000L);     // 与边车端到端预算一致
    executor.submit(() -> {
        try {
            String body = objectMapper.writeValueAsString(
                Map.of("session_id", req.sessionId(),
                       "question", req.question(),
                       "client_msg_id", req.clientMsgId()));
            // 2.7 用 RestTemplate；3.2+ 可换 RestClient
            restTemplate.execute(aiBaseUrl + "/v1/ai/chat",
                HttpMethod.POST,
                r -> { r.getHeaders().setBearerAuth(issueAiToken(userId, roleOf(userId)));
                       r.getHeaders().setContentType(MediaType.APPLICATION_JSON);
                       r.getHeaders().set("X-Trace-Id", TraceId.current());
                       r.getBody().write(body.getBytes(StandardCharsets.UTF_8)); },
                resp -> { copySse(resp.getBody(), emitter); return null; });   // 逐行转发
            emitter.complete();
        } catch (Exception e) {
            emitter.completeWithError(e);
        }
    });
    return emitter;
}
```

要点：
- **逐行转发**边车的 `event:` / `data:` 行，不要缓冲整段（否则流式白做）。
- `trace_id` 必须贯穿（Java 生成 → 边车 → 审计表），否则线上排障要两边对数。
- 超时对齐：边车端到端 8s、LLM 12s、SQL 3s；Java 侧 `SseEmitter` 超时要 ≥ 边车预算。

## 4. 版本差异（一张表）

| 关注点 | Spring Boot 2.7 | Spring Boot 3.x |
|---|---|---|
| Servlet API 包名 | `javax.servlet.*` | `jakarta.servlet.*` |
| HTTP 客户端 | `RestTemplate`（`RestClient` 不存在） | `RestTemplate` 仍可用；3.2+ 推荐 `RestClient` |
| SSE | `SseEmitter`（spring-webmvc） | 同左；WebFlux 可用 `Flux<ServerSentEvent<?>>` |
| JWT 库 | jjwt 0.11.x | jjwt 0.12.x（API 小改：`Jwts.builder().subject(...)`） |

上面的代码在两者上都能编译（除注解包名需按版本替换）。

## 5. 边车侧必须做的三件事（对应实现）

1. **只信 JWT**：`Principal(user_id=int, role)` 只能由 JWT 构造；`user_id` 为字符串/0/负数直接拒绝（`policy.Principal.__post_init__` 已实现）。
2. **连接内网**：边车不对公网暴露；若必须暴露则上 mTLS。
3. **限流配额**：Redis 令牌桶按 `user_id` 限 QPS 与日配额；超限返回友好话术而不是 429 裸错。
