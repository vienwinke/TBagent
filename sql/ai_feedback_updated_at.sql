-- ============================================================================
-- V10 · 给 ai_feedback 加 updated_at：让"评价被改过"可见
--
-- 为什么要它：反馈是"一条回答一行、可覆盖"的模型（见 sql/ai_tables.sql），
-- 原先只有 created_at，于是**用户改主意之后，表里看不出任何痕迹** ——
-- 谁在什么时候改过评价、改前是什么，都查不到（审计与灰度评估都要这个信号）。
--
-- 这里给的是**最小可用**的可观测性：updated_at > created_at ⇒ 这一行被改过。
-- （不是 append-only 审计流水：若将来需要"改前值/改了几次"，应另建一张
--   ai_feedback_log；当前需求下不值得多一张表。）
--
-- ⚠️ 为什么是 V10 而不是改 V9：V9 已在开发库被 Flyway 应用过，改它的内容会导致
--    checksum 校验失败、应用起不来。已发布的迁移只能追加。
-- ⚠️ MySQL 8 不支持 `ADD COLUMN IF NOT EXISTS`，所以本文件**不可重复执行**
--    （Flyway 保证只跑一次；手工执行前先确认列不存在）。
-- ============================================================================

ALTER TABLE ai_feedback
  ADD COLUMN updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
      ON UPDATE CURRENT_TIMESTAMP COMMENT '最近一次修改时间（> created_at 说明改过评价）';
