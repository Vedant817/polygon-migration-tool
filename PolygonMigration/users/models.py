from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.utils import timezone

class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('The Email field must be set')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        return self.create_user(email, password, **extra_fields)
    
class User(AbstractBaseUser, PermissionsMixin):
    GENDER_CHOICES = [
        ('MALE', 'Male'),
        ('FEMALE', 'Female'),
        ('OTHER', 'Other'),
    ]
    username = models.CharField(max_length=150, unique=True)
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    email = models.EmailField(unique=True, max_length=255)
    gender = models.CharField(max_length=20, choices=GENDER_CHOICES, null=True, blank=True)
    contact_number = models.CharField(max_length=20)
    college = models.CharField(max_length=255)
    graduation_year = models.IntegerField(null=True, blank=True)
    company = models.CharField(max_length=255, default="", blank=True)
    profile_picture = models.URLField(max_length=500, default="", blank=True)
    primary_coding_language = models.CharField(max_length=25, blank=True, null=True)
    linkedin_profile = models.URLField(max_length=500, default="", blank=True)
    codeforces_profile = models.URLField(max_length=500, default="", blank=True)
    leetcode_profile = models.URLField(max_length=500, default="", blank=True)
    on_going_topic = models.ForeignKey('contents.Topic', on_delete=models.SET_NULL, null=True, blank=True)
    registration_date = models.DateTimeField(default=timezone.now)
    is_premium = models.BooleanField(default=False, help_text="Whether the user has premium access")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    discord_id = models.CharField(max_length=50, null=True, blank=True, unique=True)
    discord_username = models.CharField(max_length=100, null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['first_name', 'last_name','username', 'contact_number', 'college', 'graduation_year', 'gender']

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.email})"

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'