"""pagekeep CLI."""

import typer

app = typer.Typer(help="pagekeep")


@app.command()
def hello() -> None:
    """Say hello."""
    print("Hello from pagekeep!")
