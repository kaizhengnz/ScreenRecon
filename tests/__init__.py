"""Makes the test suite a regular package.

``test_openai_provider`` imports helpers from ``tests.test_vision``. Without
this file ``tests`` resolves as a namespace package, which loses the import
lookup to any installed distribution that ships its own top-level ``tests``
package — observed with eventkit 1.0.3, which publishes its own tests.
"""
