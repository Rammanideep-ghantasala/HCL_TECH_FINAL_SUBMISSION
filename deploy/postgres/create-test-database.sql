-- A second, disposable database for the integration tests (TEST_DATABASE_URL).
-- The tests empty its assistant_* tables before every test.
CREATE DATABASE tsa_test;
