# eg4mimic
This is a project to use a raspberry pi to emulate an eg4 battery to allow a modbus polling of an inverter to use logic to start and stop the EG4 chargeverter

The first step was to use the pi to feed fake values in to the chargeverter so that we could turn it on an off in software. This should allow it to soft start and soft stop the generator (although I need to test that I think)