# How Prisma Client Python Works

This document outlines the internal workings of Prisma Client Python, based on a deep dive into the codebase.

## High-Level Overview

Prisma Client Python is a type-safe database client for Python, built on top of the Prisma ecosystem. It uses Prisma's Rust-based query engine to execute database queries, and it generates a Python client library that is tailored to your specific database schema.

The key components of the system are:

1.  **Prisma Schema:** A declarative file (`schema.prisma`) where you define your data models, database connection, and generators.
2.  **Prisma CLI:** The command-line interface for Prisma, which is used to generate the client, run migrations, and more. Prisma Client Python wraps the Prisma CLI and uses it under the hood.
3.  **Query Engine:** A Rust binary that connects to your database and executes queries.
4.  **Generated Client:** A Python library that is generated from your Prisma schema. This library provides a type-safe API for interacting with your database.

## Code Generation Process

The core of Prisma Client Python is its code generation process. Here's how it works:

1.  **`prisma generate` command:** When you run `prisma generate`, the Prisma CLI parses your `schema.prisma` file and invokes the `prisma-client-py` generator.
2.  **JSON-RPC:** The Prisma CLI communicates with the `prisma-client-py` generator using the JSON-RPC protocol over standard input/output.
3.  **`generator.py`:** The main entry point for the generator is the `Generator` class in `src/prisma/generator/generator.py`. This class receives the Prisma DMMF (Data Model Meta Format) from the Prisma CLI.
4.  **Jinja2 Templates:** The generator uses Jinja2 templates located in `src/prisma/generator/templates` to generate the Python client code. The DMMF provides the data that is used to render these templates.
5.  **Generated Files:** The templates are used to generate the following files, among others:
    *   `client.py`: The main client library, containing the `Prisma` class.
    *   `models.py`: Pydantic models for your database tables.
    *   `actions.py`: The actions that can be performed on your models (e.g., `create`, `find_many`).
    *   `types.py`: The types used in the client library.

## Key Dependencies

Prisma Client Python relies on several key dependencies:

*   **`httpx`**: For making HTTP requests to the Prisma query engine.
*   **`jinja2`**: For templating and code generation.
*   **`pydantic`**: For data validation and creating the database models.
*   **`click`**: For creating the command-line interface.
*   **`nodeenv`**: For creating an isolated Node.js environment to run the Prisma CLI.

## Project Structure

The project is structured as follows:

*   **`src/prisma`**: The core source code of the Prisma Client Python library.
    *   **`cli`**: The command-line interface.
    *   **`engine`**: The code for interacting with the Prisma query engine.
    *   **`generator`**: The code for generating the Python client.
*   **`requirements`**: The project's dependencies.
*   **`tests`**: The test suite for the project.
*   **`pyproject.toml`**: The project's configuration file, which includes settings for `ruff` (linting and formatting) and `pyright` (type checking).

This summary provides a good overview of how Prisma Client Python works. For more details, you can refer to the source code and the official Prisma documentation.
