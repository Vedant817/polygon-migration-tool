from django.apps import AppConfig


class ProblemsConfig(AppConfig):
    """
    Django AppConfig for the problems app.

    Attributes:
        default_auto_field (str): The default auto field type for models.
        name (str): The name of the app.
    """
    default_auto_field = "django.db.models.BigAutoField"
    name = "problems"
