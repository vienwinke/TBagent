-- ============================================================================
-- treatbord 内嵌 · `ai_*` 表建表脚本（P2）
--
-- 配套：docs/treatbord嵌入-接口契约.md §4（审计行字段）、
--       docs/treatbord嵌入-技术栈重设计.md §6（表设计）
--
-- ⚠️ 执行前提（这两条不在代码里，需要人工确认）：
--   1. 需要一个**可写**连接：当前边车用的是只读账号（`agent_ro` / `app_user` + 会话 READ ONLY），
--      它写不了这些表。建表与后续落库必须换一个有 DDL/DML 权限的账号，且**只给这几张表**的权限。
--   2. 边车的只读账号与落库账号要分开：问答链路继续走只读，审计写入走另一个连接，
--      否则"只读"这条硬约束会被审计写入需求侵蚀掉。
--
-- 建议执行顺序：先建表 → 再给审计账号授权（见文件末尾的 GRANT 模板）→ 最后打开落库开关。
--
-- 幂等：全部带 IF NOT EXISTS，可重复执行。
-- ============================================================================

-- 1) 会话：小程序左侧列表用
-- ⚠️ external_id 的由来（设计决策，2026-10-01）：L1/L2 契约里 session_id 是**字符串**
--    （小程序侧的会话 ID），而本表主键是 BIGINT。二者不能混用，所以：
--    · 边车持有会话（`ai_chat_session`/`ai_chat_message`），并用 external_id 承接那个字符串；
--    · 表内关联一律用 BIGINT 主键（ai_chat_message.session_id、ai_query_audit.session_id）。
--    备选方案（Java 持有会话、L2 请求里带 history）需要改已冻结的契约，故未采用。
CREATE TABLE IF NOT EXISTS ai_chat_session (
  id           BIGINT       NOT NULL AUTO_INCREMENT,
  external_id  VARCHAR(64)  NULL COMMENT 'L1/L2 契约里的字符串 session_id（外部标识）',
  user_id      BIGINT       NOT NULL,
  title        VARCHAR(64)  NOT NULL DEFAULT '',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_external (external_id),
  KEY idx_user_updated (user_id, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 会话（面向用户可见的聊天列表）';

-- 2) 消息：答案、图表、引用都在 payload 里；SQL 明文仅 ADMIN 可见（契约 §2.3）
CREATE TABLE IF NOT EXISTS ai_chat_message (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  session_id  BIGINT       NOT NULL,
  user_id     BIGINT       NOT NULL,
  role        VARCHAR(16)  NOT NULL,
  content     MEDIUMTEXT   NULL,
  payload     JSON         NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_session (session_id),
  KEY idx_user_time (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 会话消息（含图表/引用 payload）';

-- 3) ★ 审计：一次问答一行 —— 安全合规与排障的底座
--    只增不改；**永不记录敏感列的值**（问句原文按现有脱敏规则处理）
CREATE TABLE IF NOT EXISTS ai_query_audit (
  id                 BIGINT        NOT NULL AUTO_INCREMENT,
  trace_id           CHAR(32)      NOT NULL,
  user_id            BIGINT        NOT NULL,
  session_id         BIGINT        NULL,
  question           TEXT          NOT NULL,
  route              VARCHAR(16)   NULL,
  scope              VARCHAR(16)   NULL,
  detected_tables    JSON          NULL,
  generated_sql      TEXT          NULL,
  rewritten_sql      TEXT          NULL,
  policy_version     VARCHAR(16)   NULL,
  verdict            VARCHAR(16)   NULL,
  deny_reason        VARCHAR(64)   NULL,
  row_count          INT           NULL,
  truncated          TINYINT(1)    NOT NULL DEFAULT 0,
  masked_columns     JSON          NULL,
  latency_ms         INT           NULL,
  prompt_tokens      INT           NULL,
  completion_tokens  INT           NULL,
  cost_yuan          DECIMAL(10,6) NULL,
  cache_hit          TINYINT(1)    NOT NULL DEFAULT 0,
  repaired           TINYINT(1)    NOT NULL DEFAULT 0,
  model              VARCHAR(64)   NULL,
  created_at         DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_user_time (user_id, created_at),
  KEY idx_trace (trace_id),
  KEY idx_verdict_time (verdict, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 问答审计（一次问答一行，只增不改）';

-- 4) 用户反馈（👍/👎 + 可选评论）
CREATE TABLE IF NOT EXISTS ai_feedback (
  message_id  BIGINT       NOT NULL,
  user_id     BIGINT       NOT NULL,
  rating      TINYINT      NOT NULL COMMENT '1=赞 / -1=踩',
  comment     VARCHAR(255) NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (message_id),
  KEY idx_user_time (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 回答反馈';

-- 5) 提示词版本（灰度与一键回滚）
CREATE TABLE IF NOT EXISTS ai_prompt_version (
  name         VARCHAR(32)  NOT NULL,
  version      VARCHAR(16)  NOT NULL,
  content      MEDIUMTEXT   NULL,
  enabled      TINYINT(1)   NOT NULL DEFAULT 0,
  traffic_pct  TINYINT      NOT NULL DEFAULT 0,
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (name, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='提示词版本（灰度/回滚）';

-- ============================================================================
-- 授权模板（把 <AUDIT_USER> 换成落库账号；**只给这几张表**的权限）
--
--   CREATE USER IF NOT EXISTS '<AUDIT_USER>'@'%' IDENTIFIED BY '<强口令>';
--   GRANT INSERT, SELECT ON treatbord.ai_chat_session TO '<AUDIT_USER>'@'%';
--   GRANT INSERT, SELECT ON treatbord.ai_chat_message TO '<AUDIT_USER>'@'%';
--   -- 审计只增不改：**故意不给 UPDATE / DELETE**
--   GRANT INSERT, SELECT ON treatbord.ai_query_audit  TO '<AUDIT_USER>'@'%';
--   GRANT INSERT, SELECT ON treatbord.ai_feedback     TO '<AUDIT_USER>'@'%';
--   GRANT SELECT         ON treatbord.ai_prompt_version TO '<AUDIT_USER>'@'%';
--   FLUSH PRIVILEGES;
--
-- 验证（应当只有上面列出的权限）：
--   SHOW GRANTS FOR '<AUDIT_USER>'@'%';
--   INSERT INTO treatbord.ai_query_audit (trace_id, user_id, question) VALUES ('t1', 1, '自检');
--   UPDATE treatbord.ai_query_audit SET verdict='x' WHERE trace_id='t1';   -- 应当报权限错误
-- ============================================================================
