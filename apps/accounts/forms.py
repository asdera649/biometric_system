import re
from django import forms
from django.contrib.auth.forms import AuthenticationForm
from .models import CustomUser


class UserRegistrationForm(forms.ModelForm):
    """Форма регистрации нового пользователя"""
    password1 = forms.CharField(
        label='Пароль', widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Минимум 6 символов'}),
        min_length=6
    )
    password2 = forms.CharField(
        label='Повторите пароль', widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Повторите пароль'})
    )

    class Meta:
        model = CustomUser
        fields = ['username', 'last_name', 'first_name', 'patronymic', 'email', 'phone']
        labels = {
            'username': 'Логин',
            'last_name': 'Фамилия',
            'first_name': 'Имя',
            'patronymic': 'Отчество',
            'email': 'Электронная почта',
            'phone': 'Телефон',
        }
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Только латиница и цифры'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Фамилия'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Имя'}),
            'patronymic': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Отчество (необязательно)'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'example@mail.ru'}),
            'phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '+7 (999) 123-45-67'}),
        }

    def clean_username(self):
        username = self.cleaned_data.get('username', '').strip()
        if not re.match(r'^[a-zA-Z0-9_]{3,30}$', username):
            raise forms.ValidationError('Логин: 3–30 символов, только буквы латиницы, цифры и _')
        if CustomUser.objects.filter(username=username).exists():
            raise forms.ValidationError('Этот логин уже занят.')
        return username

    def clean_last_name(self):
        v = self.cleaned_data.get('last_name', '').strip()
        if not v:
            raise forms.ValidationError('Обязательное поле.')
        if not re.match(r'^[А-Яа-яЁёA-Za-z\s\-]{2,50}$', v):
            raise forms.ValidationError('Только буквы и дефис, 2–50 символов.')
        return v

    def clean_first_name(self):
        v = self.cleaned_data.get('first_name', '').strip()
        if not v:
            raise forms.ValidationError('Обязательное поле.')
        if not re.match(r'^[А-Яа-яЁёA-Za-z\s\-]{2,50}$', v):
            raise forms.ValidationError('Только буквы и дефис, 2–50 символов.')
        return v

    def clean_phone(self):
        phone = self.cleaned_data.get('phone', '').strip()
        if phone and not re.match(r'^[\+\d\s\(\)\-]{7,20}$', phone):
            raise forms.ValidationError('Некорректный формат телефона.')
        return phone

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip()
        if email and CustomUser.objects.filter(email=email).exists():
            raise forms.ValidationError('Этот email уже зарегистрирован.')
        return email

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get('password1')
        p2 = cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            self.add_error('password2', 'Пароли не совпадают.')
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data['password1'])
        if commit:
            user.save()
        return user


class FaceLoginForm(forms.Form):
    """Форма входа с биометрией"""
    username = forms.CharField(
        label='Логин',
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Введите логин', 'autofocus': True})
    )

    def clean_username(self):
        username = self.cleaned_data.get('username', '').strip()
        if not username:
            raise forms.ValidationError('Введите логин.')
        if not re.match(r'^[a-zA-Z0-9_]{1,150}$', username):
            raise forms.ValidationError('Недопустимые символы в логине.')
        return username


class UserEditForm(forms.ModelForm):
    """Форма редактирования пользователя оператором"""
    class Meta:
        model = CustomUser
        fields = ['last_name', 'first_name', 'patronymic', 'email', 'phone', 'is_operator', 'is_active']
        labels = {
            'last_name': 'Фамилия', 'first_name': 'Имя', 'patronymic': 'Отчество',
            'email': 'Email', 'phone': 'Телефон',
            'is_operator': 'Права оператора', 'is_active': 'Активен',
        }
        widgets = {
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'patronymic': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
        }
