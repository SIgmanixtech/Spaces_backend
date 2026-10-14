"""A2 spike: PostgreSQL integration tests for the proposed allocation mechanism.

These tests answer "does PostgreSQL behave the way the Phase A design assumes".
They build the proposed tables in a throwaway schema of the disposable test
database (see ``schema.sql``) and never alter the schema the application uses.
"""
