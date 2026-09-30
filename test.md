## Task 1 — Concert Ticket Cost 🎸

Write a program that asks the user for:

* Their name
* The name of a concert
* Ticket price
* Number of tickets

Then calculate the total price and print a short confirmation.

### Steps

1. Get the name with `input()`.
2. Get the concert name with `input()`.
3. Get the ticket price and convert it to `float`.
4. Get the number of tickets and convert it to `int`.
5. Calculate `total = price * tickets`.
6. Use f-strings to print the confirmation.

### Sample output

```text
Your name: Amin
Concert name: Rammstein
Ticket price: 89.50
Number of tickets: 2

Amin, your order for Rammstein is confirmed!
2 tickets × €89.50
Total: €179.00
```

---

## Task 2 — Phone Battery 🔋

Write a program that asks the user for:

* Their phone model
* Current battery percentage
* Number of hours they expect to use the phone
* Battery percentage they expect to lose per hour

Then calculate the **expected battery percentage after those hours**.

### Steps

1. Get the phone model with `input()`.
2. Get the current battery and convert it to `float`.
3. Get the number of hours and convert it to `float`.
4. Get the battery loss per hour and convert it to `float`.
5. Calculate the total expected loss.
6. Calculate the remaining battery.
7. Print the result using f-strings.

### Sample output

```text
Phone model: iPhone 15 Pro Max
Current battery (%): 82
Hours of use: 5
Battery loss per hour (%): 9.5

Your iPhone 15 Pro Max has 34.5% battery left
after 5.0 hours of use.
```
