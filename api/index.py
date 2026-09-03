"""Vercel entry point for the GLDC Flask application."""
from app import app

# Vercel's Python runtime discovers the Flask WSGI application from this module.
application = app
