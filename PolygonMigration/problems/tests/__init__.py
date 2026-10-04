"""Test suite for the problems app.

Organised as:
    polygon_stub    - a deterministic Polygon API test double (HTTP level)
    test_polygon_api - client behaviour: signing, parsing, failure handling
    test_html_parse  - problem.html parsing
    test_storage     - storage key layout, provider boundary, failure propagation
    test_migration   - full migration workflow through the view, incl. edge cases
"""