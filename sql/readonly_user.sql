-- ============================================================
-- 数据问答 Agent：最小权限只读账号（需用 root 执行一次）
-- 作用：三层护栏的第 2 层——即使 SQL 生成被绕过，数据库层也只能读
-- 用法：mysql -u root -p < sql/readonly_user.sql
-- ============================================================

-- 1) 只读账号（密码请自行替换）
CREATE USER IF NOT EXISTS 'agent_ro'@'localhost' IDENTIFIED BY 'CHANGE_ME_STRONG_PWD';
CREATE USER IF NOT EXISTS 'agent_ro'@'%'         IDENTIFIED BY 'CHANGE_ME_STRONG_PWD';

-- 2) 只给 SELECT，不给任何写权限
GRANT SELECT ON treatbord.* TO 'agent_ro'@'localhost';
GRANT SELECT ON treatbord.* TO 'agent_ro'@'%';
FLUSH PRIVILEGES;

-- 3) 可选：限制连接数与每小时查询数（防刷）
-- ALTER USER 'agent_ro'@'%' WITH MAX_USER_CONNECTIONS 10;
-- ALTER USER 'agent_ro'@'%' WITH MAX_QUERIES_PER_HOUR 2000;

-- 4) 验证（应只看到 SELECT）
SHOW GRANTS FOR 'agent_ro'@'localhost';
