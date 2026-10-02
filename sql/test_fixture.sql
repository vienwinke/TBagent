-- ============================================================================
-- **测试用最小业务库 fixture**（不是生产 schema！）
--
-- 用途：让 MySQL 专属测试能在 CI 的**空库**里跑起来 —— 这些测试在 sqlite 下会被跳过，
--       而 sqlite 没有 Decimal / MySQL 方言，正是"类型类缺陷"长期漏网的原因
--       （实测：金额列 Decimal 直接让 SSE 序列化崩掉，CI 却一直是绿的）。
--
-- 只建这些测试真正会碰到的表：
--   · task          —— 查询/行级重写路径（公开可浏览表）
--   · notification  —— 含 datetime 列，且对普通用户会被行级过滤（notification.user_id）
-- 其余表（task_claim / settlement / review …）不在子集范围内，故意不建：
--   一旦有测试碰到它们，CI 会红 —— 这正是我们要的信号，而不是静默跳过。
--
-- 生产 schema 由 treatbord 的 Flyway 迁移管理（V1~V10），本文件与它无关。
-- ============================================================================

CREATE TABLE IF NOT EXISTS task (
  id            BIGINT        NOT NULL AUTO_INCREMENT,
  publisher_id  BIGINT        NOT NULL COMMENT '发布者 user_id',
  title         VARCHAR(50)   NOT NULL,
  description   VARCHAR(2000) DEFAULT NULL,
  reward        DECIMAL(10,2) NOT NULL COMMENT '报酬（金额列：Decimal 序列化回归就靠它）',
  quota         INT           NOT NULL,
  claimed_count INT           NOT NULL DEFAULT 0,
  claim_deadline DATETIME     NOT NULL,
  deadline      DATETIME      NOT NULL,
  status        VARCHAR(20)   NOT NULL DEFAULT 'OPEN',
  version       INT           NOT NULL DEFAULT 0,
  create_time   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  update_time   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  deleted       TINYINT(1)    NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  KEY idx_status (status, deleted)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='任务（测试 fixture）';

CREATE TABLE IF NOT EXISTS notification (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  user_id     BIGINT       NOT NULL,
  type        VARCHAR(30)  NOT NULL,
  title       VARCHAR(100) DEFAULT NULL,
  content     VARCHAR(500) DEFAULT NULL,
  biz_id      BIGINT       DEFAULT NULL,
  is_read     TINYINT(1)   NOT NULL DEFAULT 0,
  create_time DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  update_time DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  deleted     TINYINT(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  KEY idx_user_time (user_id, create_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='通知（测试 fixture）';

-- 种子：金额用 DECIMAL（金额序列化回归的输入）、时间列覆盖 datetime 分支
INSERT INTO task (publisher_id, title, reward, quota, claimed_count, claim_deadline, deadline, status)
VALUES (1, '帮忙拍一张产品照片', 50.00, 3, 0, NOW() + INTERVAL 3 DAY, NOW() + INTERVAL 7 DAY, 'OPEN'),
       (1, '周末帮取快递',       15.50, 5, 1, NOW() + INTERVAL 2 DAY, NOW() + INTERVAL 5 DAY, 'OPEN'),
       (1, '翻译一份英文资料',  120.00, 1, 1, NOW() + INTERVAL 1 DAY, NOW() + INTERVAL 4 DAY, 'SETTLED');

INSERT INTO notification (user_id, type, title, content)
VALUES (2, 'CLAIMED', '接取成功', '你已接取任务'),
       (2, 'REVIEW_RESULT', '审核通过', '任务已通过审核'),
       (99, 'CLAIMED', '别人的通知', '这条不该被 user 2 看到');
