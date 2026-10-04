from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.urls import reverse

# Create your views here.
def login_view(request):
    if request.user.is_authenticated:
        logout(request)
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')
        user = authenticate(request, username=email, email=email, password=password)
        if user is not None:
            if user.is_staff:
                login(request, user)
                return redirect('problems:index')
            else:
                messages.error(request, 'You do not have staff access.')
        else:
            messages.error(request, 'Invalid email or password.')
    return render(request, 'users/login.html')

def logout_view(request):
    logout(request)
    return redirect('login')
